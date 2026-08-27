"""Pruebas del canal de Chatwoot. No salen a internet ni gastan tokens.

La API de Chatwoot se reemplaza por una de mentira que anota lo que se le
pidió. Lo que se prueba es lo nuestro: qué mensajes se contestan, cuáles no,
cómo se junta una ráfaga y qué sale para el otro lado.

El test más importante de este archivo es
`test_no_se_contesta_a_si_mismo`: sin ese filtro, cada respuesta del agente
vuelve como un mensaje nuevo y el bot se responde solo para siempre,
gastando tokens en cada vuelta.

    pytest
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agente.canales.buffer import BufferDeMensajes  # noqa: E402
from agente.canales.chatwoot import Chatwoot, _tipo_de_mensaje  # noqa: E402


def evento(
    texto="hola",
    conversacion=12,
    id_mensaje=1,
    tipo="incoming",
    privado=False,
    etiquetas=None,
    nombre="message_created",
) -> dict:
    """Un mensaje como lo manda el webhook de Chatwoot."""
    return {
        "event": nombre,
        "id": id_mensaje,
        "content": texto,
        "message_type": tipo,
        "private": privado,
        "conversation": {
            "id": conversacion,
            "status": "open",
            "labels": [] if etiquetas is None else etiquetas,
        },
        "account": {"id": 1},
        "inbox": {"id": 6, "name": "YT"},
    }


class ChatwootFalso(Chatwoot):
    """El canal, pero con la API de mentira. Anota todo lo que se mandó."""

    def __init__(self, etiqueta_humano="humano", etiquetas_remotas=None):
        super().__init__(
            url="https://chatwoot.ejemplo.com/",
            token="token-falso",
            cuenta_id=1,
            etiqueta_humano=etiqueta_humano,
        )
        self.llamadas: list[dict] = []
        self._etiquetas_remotas = etiquetas_remotas or []

    def _api(self, metodo, camino, datos=None):
        self.llamadas.append({"metodo": metodo, "camino": camino, "datos": datos})

        if camino.endswith("/labels"):
            return {"payload": self._etiquetas_remotas}
        return {"id": 99}

    def envios(self) -> list[str]:
        """Solo los mensajes que salieron para la persona."""
        return [
            l["datos"]["content"]
            for l in self.llamadas
            if l["camino"].endswith("/messages") and l["datos"]
        ]


# -- Traducir lo que llega ----------------------------------------------------


def test_traduce_un_mensaje_normal():
    entrante = ChatwootFalso().traducir(evento("¿que clima hace?", conversacion=77, id_mensaje=42))

    assert entrante is not None
    assert entrante.texto == "¿que clima hace?"
    assert entrante.conversacion == "77", "el id de conversación es el thread_id"
    assert entrante.identificador == "42"


def test_el_id_de_conversacion_va_como_texto():
    """Es el thread_id de LangGraph, y ahí un 12 y un "12" no son lo mismo."""
    entrante = ChatwootFalso().traducir(evento(conversacion=12))
    assert isinstance(entrante.conversacion, str)


@pytest.mark.parametrize(
    "nombre",
    ["conversation_created", "conversation_status_changed", "webwidget_triggered"],
)
def test_los_otros_eventos_se_dejan_pasar(nombre):
    """Chatwoot manda muchas cosas por el mismo webhook, no solo mensajes."""
    assert ChatwootFalso().traducir(evento(nombre=nombre)) is None


def test_un_mensaje_sin_texto_se_deja_pasar():
    """Un audio o una foto sueltos: el agente todavía no sabe leer eso."""
    assert ChatwootFalso().traducir(evento(texto="")) is None
    assert ChatwootFalso().traducir(evento(texto="   ")) is None


# -- Qué se contesta y qué no -------------------------------------------------


def test_no_se_contesta_a_si_mismo():
    """EL test de este archivo.

    Cada respuesta que manda el agente vuelve por el webhook como un evento
    nuevo, pero marcada como `outgoing`. Sin este filtro el agente la lee, la
    contesta, esa respuesta vuelve otra vez… y así para siempre, gastando
    tokens en cada vuelta.
    """
    canal = ChatwootFalso()
    entrante = canal.traducir(evento("mi propia respuesta", tipo="outgoing"))

    assert canal.deberia_responder(entrante) is False


def test_no_contesta_las_notas_privadas():
    """Una nota privada es para el equipo. Contestar ahí la manda al cliente."""
    canal = ChatwootFalso()
    entrante = canal.traducir(evento("ojo con este cliente", privado=True))

    assert canal.deberia_responder(entrante) is False


def test_no_contesta_dos_veces_el_mismo_mensaje():
    """Chatwoot reintenta el webhook si no le contestamos a tiempo."""
    canal = ChatwootFalso()
    entrante = canal.traducir(evento(id_mensaje=7))

    assert canal.deberia_responder(entrante) is True
    assert canal.deberia_responder(entrante) is False, "la segunda vez ya no"


def test_la_etiqueta_apaga_el_bot():
    """El traspaso a una persona: se pone la etiqueta y el bot se calla."""
    canal = ChatwootFalso()
    entrante = canal.traducir(evento(etiquetas=["humano"]))

    assert canal.deberia_responder(entrante) is False


def test_la_etiqueta_no_distingue_mayusculas():
    canal = ChatwootFalso()
    assert canal.deberia_responder(canal.traducir(evento(etiquetas=["Humano"]))) is False


def test_otras_etiquetas_no_lo_apagan():
    canal = ChatwootFalso()
    entrante = canal.traducir(evento(etiquetas=["ventas", "urgente"]))

    assert canal.deberia_responder(entrante) is True


def test_si_el_evento_no_trae_etiquetas_se_las_pregunta():
    """Dar por hecho que no hay ninguna sería hablar arriba de una persona."""
    canal = ChatwootFalso(etiquetas_remotas=["humano"])
    crudo = evento()
    del crudo["conversation"]["labels"]

    assert canal.deberia_responder(canal.traducir(crudo)) is False
    assert any(l["camino"].endswith("/labels") for l in canal.llamadas)


def test_un_mensaje_normal_se_contesta():
    canal = ChatwootFalso()
    assert canal.deberia_responder(canal.traducir(evento())) is True


@pytest.mark.parametrize(
    "crudo,esperado",
    [({"message_type": 0}, "incoming"), ({"message_type": 1}, "outgoing")],
)
def test_el_tipo_tambien_se_entiende_como_numero(crudo, esperado):
    """Según la versión, Chatwoot lo manda como texto o como número."""
    assert _tipo_de_mensaje(crudo) == esperado


# -- Lo que sale --------------------------------------------------------------


def test_envia_cada_mensaje_por_separado():
    canal = ChatwootFalso()

    canal.enviar("12", ["primero", "segundo"])

    assert canal.envios() == ["primero", "segundo"]


def test_lo_que_sale_va_marcado_como_outgoing():
    """Si no, Chatwoot no lo empuja a WhatsApp y queda solo en la bandeja."""
    canal = ChatwootFalso()

    canal.enviar("12", ["hola"])

    envio = [l for l in canal.llamadas if l["camino"].endswith("/messages")][0]
    assert envio["datos"]["message_type"] == "outgoing"
    assert envio["camino"] == "conversations/12/messages"


def test_no_manda_mensajes_vacios():
    canal = ChatwootFalso()

    canal.enviar("12", ["hola", "   ", ""])

    assert canal.envios() == ["hola"]


def test_la_barra_final_de_la_url_no_se_duplica():
    """Con "...com//api/v1" Chatwoot devuelve 404 y no se entiende por qué."""
    canal = ChatwootFalso()
    assert canal.url == "https://chatwoot.ejemplo.com"


def test_el_escribiendo_no_voltea_la_respuesta(monkeypatch):
    """Es cosmético: si falla, la respuesta tiene que salir igual."""
    canal = ChatwootFalso()

    def explota(*a, **k):
        raise ConnectionError("se cayó")

    monkeypatch.setattr(canal, "_api", explota)
    canal.escribiendo("12")  # no tiene que levantar nada


def test_sin_datos_avisa_que_faltan():
    with pytest.raises(ValueError, match="CHATWOOT_URL"):
        Chatwoot(url="", token="", cuenta_id=1)


# -- Juntar la ráfaga ---------------------------------------------------------


def juntar(buffer: BufferDeMensajes, mensajes, conversacion="12", entre=0.0):
    """Manda varios mensajes seguidos y espera a que el buffer los suelte."""

    async def correr():
        for texto in mensajes:
            await buffer.agregar(conversacion, texto)
            if entre:
                await asyncio.sleep(entre)
        # Un rato más que la espera del buffer, para que llegue a soltar.
        await asyncio.sleep(buffer.segundos + 0.15)

    asyncio.run(correr())


def test_tres_mensajes_seguidos_son_una_sola_respuesta():
    """El caso de todos los días: "hola" / "una consulta" / "por el precio"."""
    sueltos = []
    buffer = BufferDeMensajes(
        0.05, lambda c, t: _anotar(sueltos, c, t)
    )

    juntar(buffer, ["hola", "una consulta", "por el precio"])

    assert len(sueltos) == 1, "tendría que haber contestado una sola vez"
    assert sueltos[0] == ("12", "hola\nuna consulta\npor el precio")


def test_cada_conversacion_junta_la_suya():
    """Si se mezclaran, una persona recibiría el mensaje de otra."""
    sueltos = []
    buffer = BufferDeMensajes(0.05, lambda c, t: _anotar(sueltos, c, t))

    async def correr():
        await buffer.agregar("111", "soy uno")
        await buffer.agregar("222", "soy dos")
        await asyncio.sleep(0.25)

    asyncio.run(correr())

    assert sorted(sueltos) == [("111", "soy uno"), ("222", "soy dos")]


def test_sin_espera_contesta_derecho():
    """BUFFER_SEGUNDOS=0 apaga el buffer."""
    sueltos = []
    buffer = BufferDeMensajes(0, lambda c, t: _anotar(sueltos, c, t))

    asyncio.run(buffer.agregar("12", "hola"))

    assert sueltos == [("12", "hola")]


def test_el_tope_corta_la_espera_infinita():
    """Sin tope, alguien que escribe de a poco no recibe respuesta nunca.

    Cada mensaje reinicia el reloj. Si la persona manda uno justo antes de
    que se cumpla la espera, y otro, y otro, el agente se quedaría esperando
    para siempre. El tope obliga a contestar igual.
    """
    sueltos = []
    buffer = BufferDeMensajes(
        0.20, lambda c, t: _anotar(sueltos, c, t), tope=0.30
    )

    # Seis mensajes cada 0,1s = 0,6s de charla. Con la espera de 0,2s
    # reiniciándose cada vez, sin tope no soltaría hasta el final.
    juntar(buffer, ["a", "b", "c", "d", "e", "f"], entre=0.1)

    assert sueltos, "el tope tendría que haber forzado la respuesta"
    assert len(sueltos[0][1].split("\n")) < 6, "soltó antes de que terminara"


def test_al_apagar_no_se_pierde_lo_que_estaba_esperando():
    """Un deploy justo en esos segundos dejaría a alguien sin respuesta."""
    sueltos = []
    buffer = BufferDeMensajes(30, lambda c, t: _anotar(sueltos, c, t))

    async def correr():
        await buffer.agregar("12", "hola")
        assert buffer.pendientes("12") == 1
        await buffer.vaciar()

    asyncio.run(correr())

    assert sueltos == [("12", "hola")]


async def _anotar(donde, conversacion, texto):
    donde.append((conversacion, texto))


# -- El webhook entero, de punta a punta --------------------------------------
#
# Acá se pegan todas las piezas: llega el pedido de Chatwoot, contesta el
# agente, sale la respuesta. Con un canal de mentira y un modelo de mentira,
# así que no toca la red ni gasta un token.

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def cliente(canal, agente, token="secreto", buffer_segundos=0):
    """Levanta el webhook con las piezas de mentira adentro."""
    pytest.importorskip("httpx", reason="TestClient de FastAPI necesita httpx")
    from fastapi.testclient import TestClient

    from agente.web.webhook import crear_app

    from test_agente import agente_falso  # noqa: F401  (deja src en sys.path)

    config = agente.config
    config.chatwoot_webhook_token = token
    config.buffer_segundos = buffer_segundos

    return TestClient(crear_app(config, agente=agente, canal=canal))


def test_el_mensaje_da_la_vuelta_completa():
    from test_agente import agente_falso

    canal = ChatwootFalso()
    agente = agente_falso(["¡Buenas! ¿En qué te ayudo?"])

    with cliente(canal, agente) as web:
        respuesta = web.post("/chatwoot/secreto", json=evento("hola", conversacion=55))

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "recibido"
    assert canal.envios() == ["¡Buenas! ¿En qué te ayudo?"]


def test_con_el_token_equivocado_no_entra():
    """Es lo único que separa a Chatwoot de cualquiera que sepa el dominio."""
    from test_agente import agente_falso

    canal = ChatwootFalso()

    with cliente(canal, agente_falso(["hola"])) as web:
        respuesta = web.post("/chatwoot/no-es-este", json=evento())

    assert respuesta.status_code == 401
    assert canal.envios() == [], "no tiene que haber contestado nada"


def test_su_propia_respuesta_no_dispara_otra():
    """El ida y vuelta infinito, probado de punta a punta."""
    from test_agente import agente_falso

    canal = ChatwootFalso()
    agente = agente_falso(["no debería usarse"])

    with cliente(canal, agente) as web:
        respuesta = web.post("/chatwoot/secreto", json=evento(tipo="outgoing"))

    assert respuesta.json()["estado"] == "ignorado"
    assert canal.envios() == []


def test_si_el_modelo_falla_se_le_avisa_a_la_persona():
    """Un error con una persona no puede dejarla esperando en silencio."""
    from test_agente import agente_falso

    canal = ChatwootFalso()
    agente = agente_falso([])  # sin respuestas: el modelo falso revienta

    with cliente(canal, agente) as web:
        web.post("/chatwoot/secreto", json=evento("hola"))

    assert len(canal.envios()) == 1
    assert "rompió" in canal.envios()[0]


def test_el_salud_contesta():
    """Coolify le pega a esto; si no contesta, reinicia el contenedor."""
    from test_agente import agente_falso

    with cliente(ChatwootFalso(), agente_falso(["hola"])) as web:
        respuesta = web.get("/salud")

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "ok"
