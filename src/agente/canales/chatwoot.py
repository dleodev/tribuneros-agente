"""El canal de Chatwoot.

Es el que atiende WhatsApp de verdad, pero no le habla a Meta: le habla a
Chatwoot, que está en el medio. El recorrido completo de un mensaje es este:

    persona → WhatsApp → Meta → Chatwoot → (webhook) → agente
                                    ↑                     │
                                    └───── API REST ──────┘

Por qué con Chatwoot en el medio y no directo contra Meta:

  · Queda el historial y la bandeja de entrada, con buscador.
  · Una persona puede meterse en la conversación y seguirla a mano.
  · El mismo agente atiende Instagram, el widget de la web o Telegram sin
    tocar una línea: para nosotros todo entra por el mismo webhook.

A diferencia de Telegram, acá **nadie sale a buscar los mensajes**: Chatwoot
nos pega a una URL cuando pasa algo. Por eso esto necesita un servidor con
dominio y HTTPS, y por eso el webhook vive en su propia app (web/webhook.py).

El `conversacion` (el thread_id de LangGraph) es el **id de conversación de
Chatwoot**. Es la misma unidad que ves en la bandeja: un hilo en la pantalla
es un hilo de memoria del agente. También es lo que necesitamos para
contestar, así que sirve para las dos cosas.

Se usa `urllib`, de la biblioteca estándar, para no sumar una dependencia.
La API de Chatwoot son pedidos HTTP con JSON: no hace falta más.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque

from .base import Canal, MensajeEntrante

log = logging.getLogger(__name__)

# Cuánto esperamos a que Chatwoot conteste. Corre en el mismo servidor que
# el agente, así que si tarda más que esto es porque algo anda mal.
ESPERA_DE_RED = 20

# Las fotos de producto. El agente las pide con una línea `[foto: https://...]`
# dentro de su respuesta (se la da la herramienta de productos). Solo se
# descargan de la CDN de Tiendanube: el modelo no puede mandar al servidor a
# bajar cualquier dirección, que sería un agujero.
ETIQUETA_DE_FOTO = re.compile(r"\[foto:\s*(https?://[^\s\]]+)\]", re.IGNORECASE)
MAXIMO_DE_FOTOS = 3
PESO_MAXIMO_DE_FOTO = 5 * 1024 * 1024
TIPOS_DE_FOTO = {"image/jpeg": "jpg", "image/png": "png"}


def separar_fotos(texto: str) -> tuple[str, list[str]]:
    """Saca las etiquetas de foto del texto. Devuelve (texto limpio, [urls])."""
    fotos = ETIQUETA_DE_FOTO.findall(texto)
    return ETIQUETA_DE_FOTO.sub("", texto).strip(), fotos


def _es_de_tiendanube(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host == "mitiendanube.com" or host.endswith(".mitiendanube.com")


class ErrorDeChatwoot(Exception):
    """Chatwoot contestó algo que no esperábamos."""


class Chatwoot(Canal):
    """La bandeja de Chatwoot: por acá entran y salen los mensajes."""

    nombre = "chatwoot"

    def __init__(
        self,
        url: str,
        token: str,
        cuenta_id: str | int,
        etiqueta_humano: str = "humano",
    ) -> None:
        if not url or not token:
            raise ValueError(
                "Faltan datos de Chatwoot. Abrí el .env y completá "
                "CHATWOOT_URL y CHATWOOT_TOKEN."
            )

        # La barra final sobra y duplicada rompe la URL ("...com//api/v1").
        self.url = url.rstrip("/")
        self.token = token
        self.cuenta_id = str(cuenta_id)
        self.etiqueta_humano = (etiqueta_humano or "").strip().lower()

        # Los mensajes que ya contestamos. Chatwoot reintenta el webhook si no
        # le respondemos rápido, y sin esto el agente contesta dos veces lo
        # mismo. Alcanza con acordarse de los últimos.
        self._ya_contestados: deque[str] = deque(maxlen=1000)

    # -- Entrada ---------------------------------------------------------------

    def traducir(self, evento: dict) -> MensajeEntrante | None:
        """Convierte un evento del webhook en algo que el agente entiende.

        Devuelve None si el evento no es un mensaje que tengamos que mirar:
        otro tipo de evento, o un mensaje sin texto (un audio, una foto, un
        adjunto suelto) que el agente todavía no sabe leer.
        """
        if evento.get("event") != "message_created":
            return None

        conversacion = evento.get("conversation") or {}
        id_conversacion = conversacion.get("id")
        texto = (evento.get("content") or "").strip()

        if not id_conversacion or not texto:
            return None

        return MensajeEntrante(
            texto=texto,
            # El id de conversación es el thread_id: la memoria de cada
            # persona por separado.
            conversacion=str(id_conversacion),
            identificador=str(evento.get("id") or ""),
            datos=evento,
        )

    def deberia_responder(self, mensaje: MensajeEntrante) -> bool:
        """Si el agente tiene que contestar este mensaje o dejarlo pasar.

        Acá está casi toda la diferencia entre un bot de demo y uno que
        atiende clientes de verdad. Son cuatro filtros y los cuatro importan:
        """
        evento = mensaje.datos

        # 1. Solo los mensajes que ENTRAN. Los que salen son las respuestas
        #    del propio agente y las de las personas del equipo. Sin este
        #    filtro el agente se lee a sí mismo y se contesta para siempre:
        #    es el error más caro de todos, porque cada vuelta gasta tokens.
        if _tipo_de_mensaje(evento) != "incoming":
            return False

        # 2. Las notas privadas son para el equipo, no para el cliente. Si el
        #    agente contestara ahí, mandaría al chat algo que era interno.
        if evento.get("private"):
            return False

        # 3. El mismo mensaje dos veces. Chatwoot reintenta si el webhook no
        #    contestó a tiempo, y el reintento trae el mismo id.
        if mensaje.identificador and mensaje.identificador in self._ya_contestados:
            return False

        # 4. El traspaso a una persona. Si la conversación tiene la etiqueta,
        #    el bot se calla: la está atendiendo alguien del equipo. Es *el*
        #    diferencial de tener Chatwoot en el medio — se apaga con un clic
        #    desde la bandeja, sin tocar el servidor.
        if self._la_atiende_una_persona(evento):
            return False

        if mensaje.identificador:
            self._ya_contestados.append(mensaje.identificador)

        return True

    def _la_atiende_una_persona(self, evento: dict) -> bool:
        """Si la conversación está marcada con la etiqueta de traspaso."""
        if not self.etiqueta_humano:
            return False

        conversacion = evento.get("conversation") or {}
        etiquetas = conversacion.get("labels")

        # Chatwoot manda las etiquetas en el evento casi siempre. Cuando no
        # las manda (cambia entre versiones y entre tipos de evento) hay que
        # preguntarle, porque dar por hecho que no hay ninguna sería dejar al
        # bot hablando arriba de una persona.
        if etiquetas is None:
            etiquetas = self._etiquetas_de(conversacion.get("id"))

        return self.etiqueta_humano in {
            str(e).strip().lower() for e in etiquetas or []
        }

    def _etiquetas_de(self, id_conversacion) -> list[str]:
        """Le pregunta a Chatwoot qué etiquetas tiene una conversación."""
        if not id_conversacion:
            return []

        # Por qué se traga el error: si Chatwoot no contesta esta consulta, la
        # alternativa es no responderle al cliente. Preferimos responder. El
        # riesgo del otro lado (el bot habla arriba de una persona) existe,
        # pero solo en el caso raro de que justo esta llamada falle.
        try:
            respuesta = self._api(
                "GET", f"conversations/{id_conversacion}/labels"
            )
        except Exception:
            return []

        return respuesta.get("payload") or []

    # -- Salida ----------------------------------------------------------------

    def enviar(self, conversacion: str, mensajes: list[str]) -> None:
        """Manda las respuestas a esa conversación, en orden.

        Salen como `outgoing`, que es lo que Chatwoot entiende por "esto lo
        dice nuestro lado". Desde ahí Chatwoot lo empuja al canal que
        corresponda: WhatsApp, Instagram, el widget de la web.
        """
        for texto in mensajes:
            texto, fotos = separar_fotos(texto)

            if texto:
                self._api(
                    "POST",
                    f"conversations/{conversacion}/messages",
                    {"content": texto, "message_type": "outgoing"},
                )

            for foto in fotos[:MAXIMO_DE_FOTOS]:
                # Una foto que no sale no tiene que tirar abajo la respuesta:
                # el texto (con el link del producto) ya se mandó.
                try:
                    self._enviar_foto(conversacion, foto)
                except Exception as e:  # noqa: BLE001
                    log.warning("No se pudo mandar la foto %s: %s", foto, e)

    def _enviar_foto(self, conversacion: str, url: str) -> None:
        """Descarga la foto y la sube a la conversación como adjunto."""
        if not _es_de_tiendanube(url):
            raise ValueError("la foto no es de la CDN de Tiendanube")

        pedido = urllib.request.Request(
            url,
            headers={
                # La CDN rechaza (403) los pedidos que no parecen de un navegador.
                "User-Agent": "Mozilla/5.0 (compatible; Tribuneros)",
                "Referer": "https://tribuneros.store/",
            },
        )
        with urllib.request.urlopen(pedido, timeout=ESPERA_DE_RED) as respuesta:
            tipo = respuesta.headers.get_content_type()
            contenido = respuesta.read(PESO_MAXIMO_DE_FOTO + 1)
        if tipo not in TIPOS_DE_FOTO:
            raise ValueError(f"tipo de imagen no soportado: {tipo}")
        if len(contenido) > PESO_MAXIMO_DE_FOTO:
            raise ValueError("la foto pesa demasiado")

        self._api_con_archivo(
            f"conversations/{conversacion}/messages",
            archivo=(f"foto.{TIPOS_DE_FOTO[tipo]}", tipo, contenido),
        )

    def _api_con_archivo(self, camino: str, archivo: tuple[str, str, bytes]) -> dict:
        """Un POST multipart con un adjunto: así Chatwoot recibe archivos."""
        nombre, tipo, contenido = archivo
        limite = uuid.uuid4().hex
        cuerpo = b"".join(
            [
                f"--{limite}\r\nContent-Disposition: form-data; name=\"message_type\"\r\n\r\noutgoing\r\n".encode(),
                f"--{limite}\r\nContent-Disposition: form-data; name=\"attachments[]\"; "
                f"filename=\"{nombre}\"\r\nContent-Type: {tipo}\r\n\r\n".encode(),
                contenido,
                f"\r\n--{limite}--\r\n".encode(),
            ]
        )
        pedido = urllib.request.Request(
            f"{self.url}/api/v1/accounts/{self.cuenta_id}/{camino}",
            data=cuerpo,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={limite}",
                "api_access_token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(pedido, timeout=ESPERA_DE_RED) as respuesta:
                crudo = respuesta.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detalle = e.read().decode("utf-8", "replace")[:300]
            raise ErrorDeChatwoot(
                f"Chatwoot devolvió {e.code} al subir una foto a {camino}: {detalle}"
            ) from None
        return json.loads(crudo) if crudo else {}

    def escribiendo(self, conversacion: str, encendido: bool = True) -> None:
        """El "escribiendo..." mientras el modelo piensa.

        No es decorativo: una respuesta puede tardar varios segundos y sin
        esto la persona no sabe si la escucharon o si se colgó.
        """
        # Que falle el aviso no puede voltear la respuesta: es cosmético.
        try:
            self._api(
                "POST",
                f"conversations/{conversacion}/toggle_typing_status",
                {"typing_status": "on" if encendido else "off"},
            )
        except Exception:
            pass

    # -- La API ----------------------------------------------------------------

    def yo_soy(self) -> dict:
        """Los datos de la cuenta. Sirve para avisar al arrancar con cuál se habla."""
        return self._api("GET", "conversations?status=open&page=1")

    def _api(self, metodo: str, camino: str, datos: dict | None = None) -> dict:
        """Una llamada a la API de Chatwoot."""
        url = f"{self.url}/api/v1/accounts/{self.cuenta_id}/{camino}"

        pedido = urllib.request.Request(
            url,
            data=json.dumps(datos).encode("utf-8") if datos is not None else None,
            method=metodo,
            headers={
                "Content-Type": "application/json",
                # Así se autentica Chatwoot: no es un Bearer, es este header.
                "api_access_token": self.token,
            },
        )

        try:
            with urllib.request.urlopen(pedido, timeout=ESPERA_DE_RED) as respuesta:
                cuerpo = respuesta.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # El cuerpo del error es lo único que dice qué pasó de verdad
            # (token vencido, conversación que no existe, cuenta equivocada).
            # Sin esto solo se ve "HTTP Error 404" y no se puede arreglar nada.
            detalle = e.read().decode("utf-8", "replace")[:300]
            raise ErrorDeChatwoot(
                f"Chatwoot devolvió {e.code} en {metodo} {camino}: {detalle}"
            ) from None

        return json.loads(cuerpo) if cuerpo else {}


# -- Ayudantes ----------------------------------------------------------------


def _tipo_de_mensaje(evento: dict) -> str:
    """Si el mensaje entra o sale.

    Chatwoot lo manda como texto ("incoming"), pero según la versión y el
    endpoint puede venir como número (0 = incoming, 1 = outgoing). Traducimos
    los dos para que un cambio de versión no vuelva loco al bot.
    """
    tipo = evento.get("message_type")

    if isinstance(tipo, int):
        return {0: "incoming", 1: "outgoing"}.get(tipo, "otro")

    return str(tipo or "").strip().lower()
