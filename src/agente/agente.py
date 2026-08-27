"""El agente.

Esta es la pieza central y es corta a propósito. Un agente conversacional
son tres cosas juntas:

    1. Un prompt de sistema  → quién es y cómo se comporta
    2. Una memoria           → de qué vienen hablando
    3. Un modelo             → a quién le mandás las dos cosas de arriba

Con LangGraph esas tres cosas se arman como un grafo. Acá el grafo es un ciclo
de dos nodos:

    modelo → ¿pidió una herramienta?
               sí → herramientas → vuelve al modelo
               no → listo, contesta

Mientras el modelo no pida nada, el camino es el de siempre: un solo paso y
responde. Las herramientas viven en herramientas.py.

El agente no sabe si lo están usando desde la terminal, desde la web o desde
WhatsApp. Recibe texto y devuelve texto. Esa frontera es lo que después
permite enchufarlo a cualquier canal sin tocar una línea de acá adentro.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .config import Config
from .herramientas import HERRAMIENTAS
from .memoria import crear_memoria
from .modelos import crear_modelo
from .prompts import leer_prompt
from .respuesta import partir_respuesta


@dataclass
class Respuesta:
    """Lo que devuelve el agente cuando termina de responder."""

    texto: str
    modelo: str
    tokens_entrada: int = 0
    tokens_salida: int = 0
    # Del total de entrada, cuántos salieron del caché (baratos) y cuántos
    # se guardaron en el caché para la próxima.
    tokens_cache_leidos: int = 0
    tokens_cache_guardados: int = 0


class Transmision:
    """Una respuesta que llega de a pedacitos.

    Se recorre con un for y, cuando termina, deja el resumen en `.resumen`:

        transmision = agente.responder_en_vivo("hola")
        for pedazo in transmision:
            print(pedazo, end="")
        print(transmision.resumen.tokens_salida)

    Por qué existe, en vez de guardar el resumen en el agente: porque el
    mismo agente puede estar atendiendo varias conversaciones al mismo
    tiempo. Si el resumen viviera en el agente, dos conversaciones que
    responden a la vez se pisarían los datos. Acá cada transmisión tiene
    el suyo.
    """

    def __init__(self, pedazos, al_terminar) -> None:
        self._pedazos = pedazos
        self._al_terminar = al_terminar
        self._partes: list[str] = []
        self._terminada = False
        self.resumen: Respuesta | None = None

    def __iter__(self):
        # Una transmisión se consume una sola vez: los pedazos llegan del
        # modelo y no vuelven. Si ya la recorriste, te devolvemos lo que
        # quedó guardado en vez de un vacío silencioso.
        if self._terminada:
            yield from self._partes
            return

        for pedazo in self._pedazos:
            self._partes.append(pedazo)
            yield pedazo

        self._terminada = True
        self.resumen = self._al_terminar()
        self.resumen.texto = "".join(self._partes)

    def texto(self) -> str:
        """El texto completo. Consume la transmisión si todavía no la recorriste."""
        return "".join(self)


class Agente:
    def __init__(self, config: Config | None = None, checkpointer=None) -> None:
        self.config = config or Config.desde_entorno()

        self.modelo = crear_modelo(
            proveedor=self.config.proveedor,
            api_key=self.config.api_key,
            modelo=self.config.modelo,
            max_tokens=self.config.max_tokens,
        )

        # La memoria: SQLite si MODO=test, Postgres si MODO=produccion.
        # Se le puede pasar otra a mano (los tests le pasan una en RAM).
        self.checkpointer = checkpointer or crear_memoria(self.config)

        self.grafo = self._construir_grafo()

    # -- El grafo -------------------------------------------------------------

    def _construir_grafo(self):
        """Arma el grafo: el modelo, las herramientas, y la vuelta al modelo."""

        # bind_tools() es lo que le avisa al modelo qué herramientas existe.
        # Sin esto nunca las pide, por más que estén escritas.
        modelo = self.modelo.bind_tools(HERRAMIENTAS)

        def nodo_modelo(estado: MessagesState) -> dict:
            respuesta = modelo.invoke(self._armar_entrada(estado))
            return {"messages": [respuesta]}

        grafo = StateGraph(MessagesState)
        grafo.add_node("modelo", nodo_modelo)
        grafo.add_node("herramientas", ToolNode(HERRAMIENTAS))
        grafo.add_edge(START, "modelo")

        # tools_condition mira la respuesta del modelo: si pidió herramientas
        # devuelve "tools", y si no, END. Como nuestro nodo se llama en
        # castellano, le pasamos el mapa para traducir esa salida.
        grafo.add_conditional_edges(
            "modelo",
            tools_condition,
            {"tools": "herramientas", END: END},
        )

        # Y con el resultado en la mano, el modelo vuelve a hablar. Este es el
        # ciclo: puede pedir varias herramientas seguidas antes de contestar.
        grafo.add_edge("herramientas", "modelo")

        return grafo.compile(checkpointer=self.checkpointer)

    def _armar_entrada(self, estado: MessagesState) -> list:
        """Prompt de sistema + los últimos mensajes de la conversación.

        El prompt se lee del archivo en CADA mensaje, no una sola vez al
        arrancar: por eso podés editar prompts/sistema.md sin reiniciar.
        """
        recientes = _recortar(estado["messages"], self.config.memoria_mensajes)
        return [self._sistema(), *recientes]

    def _sistema(self) -> SystemMessage:
        """El prompt del sistema, marcado para que el proveedor lo cachee.

        El prompt viaja entero en CADA mensaje. Si es largo, lo estás pagando
        una y otra vez. El caché hace que el proveedor lo guarde de su lado y
        te cobre una fracción a partir del segundo mensaje.

        Cada proveedor lo maneja distinto:
          · Claude → hay que marcarlo a mano (es lo que hacemos acá abajo)
          · OpenAI → automático, no hay que hacer nada
          · Gemini → automático, no hay que hacer nada

        Ojo: el caché recién se activa cuando el prompt supera cierto tamaño
        (más o menos 1.000 tokens). Con un prompt corto no pasa nada malo,
        simplemente no se cachea.
        """
        texto = leer_prompt(self.config.prompt_sistema)

        if self.config.cache and self.config.proveedor == "claude":
            return SystemMessage(
                content=[
                    {
                        "type": "text",
                        "text": texto,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            )

        return SystemMessage(texto)

    # -- Lo que usa todo el mundo --------------------------------------------

    def responder(self, texto: str, conversacion: str = "local") -> Respuesta:
        """Le mandás un mensaje, te devuelve la respuesta completa.

        `conversacion` es el thread_id de LangGraph: cada valor distinto es
        una conversación separada, con su propia memoria. En Telegram o
        WhatsApp acá va el número o el chat_id de la persona.
        """
        salida = self.grafo.invoke(
            {"messages": [HumanMessage(texto)]},
            config=self._config_hilo(conversacion),
        )
        return _a_respuesta(salida["messages"][-1], self.config.modelo)

    def responder_en_vivo(
        self, texto: str, conversacion: str = "local"
    ) -> Transmision:
        """Igual que responder(), pero el texto llega mientras se escribe.

        Devuelve una Transmision: la recorrés con un for y al terminar tenés
        el resumen (tokens, modelo) en `.resumen`.
        """
        # Cada transmisión guarda su propio acumulado. Nada de estado en el
        # agente: puede haber muchas conversaciones respondiendo a la vez.
        acumulado: list = [None]

        def pedazos() -> Iterator[str]:
            for pedazo, _ in self.grafo.stream(
                {"messages": [HumanMessage(texto)]},
                config=self._config_hilo(conversacion),
                stream_mode="messages",
            ):
                # Por acá también pasa lo que devuelven las herramientas, y eso
                # no es la respuesta: es materia prima para que el modelo la
                # escriba. Si lo dejáramos salir, la persona vería el listado
                # crudo del clima en pantalla y después la respuesta de verdad.
                if isinstance(pedazo, ToolMessage):
                    continue

                # Los pedazos de LangChain se suman entre sí. Al sumarlos
                # vamos rearmando el mensaje entero, con el conteo de tokens
                # incluido (que en varios proveedores llega recién al final).
                # Cuando hay herramientas el modelo habla dos veces, así que
                # esta suma termina juntando el gasto de las dos llamadas:
                # que es justo lo que costó la respuesta.
                acumulado[0] = (
                    pedazo
                    if acumulado[0] is None
                    else _sumar(acumulado[0], pedazo)
                )

                trozo = _texto_de(pedazo)
                if trozo:
                    yield trozo

        return Transmision(
            pedazos(),
            lambda: _a_respuesta(acumulado[0], self.config.modelo),
        )

    # -- Utilidades -----------------------------------------------------------

    def responder_partido(
        self, texto: str, conversacion: str = "local"
    ) -> list[str]:
        """Como responder(), pero la respuesta ya viene partida en mensajes.

        Es lo que van a usar Telegram y WhatsApp: en mensajería una respuesta
        larga se manda en varios globos cortos, no en un ladrillo.
        """
        return partir_respuesta(self.responder(texto, conversacion).texto)

    def historial(self, conversacion: str = "local") -> list:
        """Los mensajes guardados de una conversación."""
        estado = self.grafo.get_state(self._config_hilo(conversacion))
        return estado.values.get("messages", []) if estado.values else []

    def olvidar(self, conversacion: str = "local") -> None:
        """Borra de verdad una conversación: el agente arranca de cero con esa persona.

        Ojo: no alcanza con crear un agente nuevo. La conversación no vive en
        el agente, vive en el checkpointer — si no la borrás de ahí, el agente
        nuevo la vuelve a levantar y parece que no pasó nada.
        """
        self.checkpointer.delete_thread(conversacion)

    def _config_hilo(self, conversacion: str) -> dict:
        return {"configurable": {"thread_id": conversacion}}


# -- Ayudantes ----------------------------------------------------------------


def _recortar(mensajes: list, tope: int) -> list:
    """Los últimos mensajes de la conversación, cortando en un turno completo.

    Acá había un trim_messages() de LangChain, que recorta contando mensajes
    sueltos. Con herramientas eso se rompe, y es la trampa más cara de este
    proyecto: una vuelta de herramienta son tres mensajes atados entre sí
    (el modelo la pide, la herramienta contesta, el modelo responde), y los
    proveedores exigen que estén los tres. Si el corte cae justo en el medio y
    deja un pedido sin su resultado, la API devuelve un 400 que no explica
    nada y aparece recién cuando la conversación se hizo larga.

    Por eso no recortamos por mensaje sino por turno: agrupamos y siempre
    entran o salen enteros. `tope` sigue siendo en mensajes (MEMORIA_MENSAJES),
    así que el número del .env significa lo mismo que antes.
    """
    turnos = _partir_en_turnos(mensajes)

    elegidos: list[list] = []
    total = 0

    for turno in reversed(turnos):
        # El turno más nuevo entra siempre, aunque se pase del tope: es la
        # pregunta que estamos respondiendo recién ahora. Dejarlo afuera por
        # el presupuesto sería mandarle al modelo una conversación sin la
        # consulta — o peor, partida justo por la mitad de una herramienta.
        if elegidos and total + len(turno) > tope:
            break

        elegidos.insert(0, turno)
        total += len(turno)

    return [mensaje for turno in elegidos for mensaje in turno]


def _partir_en_turnos(mensajes: list) -> list[list]:
    """Agrupa la conversación en turnos.

    Un turno arranca en un mensaje de la persona y se lleva todo lo que se
    generó a partir de él: los pedidos de herramienta, sus resultados y la
    respuesta final. Es la unidad que no se puede partir al recortar.
    """
    turnos: list[list] = []

    for mensaje in mensajes:
        # El `or not turnos` es para el caso raro de que la conversación no
        # empiece con la persona: así el primer mensaje no se pierde.
        if isinstance(mensaje, HumanMessage) or not turnos:
            turnos.append([mensaje])
        else:
            turnos[-1].append(mensaje)

    return turnos


def _sumar(acumulado, pedazo):
    """Suma dos pedazos de respuesta. Si el proveedor manda algo raro, sigue."""
    try:
        return acumulado + pedazo
    except (TypeError, ValueError):
        return pedazo


def _texto_de(mensaje) -> str:
    """Saca el texto de un pedazo de respuesta.

    Hace falta porque no todos los proveedores devuelven lo mismo: algunos
    mandan un string y otros una lista de bloques.
    """
    if mensaje is None:
        return ""

    contenido = getattr(mensaje, "content", "")

    if isinstance(contenido, str):
        return contenido

    if isinstance(contenido, list):
        return "".join(
            b.get("text", "")
            for b in contenido
            if isinstance(b, dict) and b.get("type") == "text"
        )

    return ""


def _a_respuesta(mensaje, modelo_por_defecto: str) -> Respuesta:
    """Convierte la respuesta de LangChain en algo simple de usar."""
    if mensaje is None:
        return Respuesta(texto="", modelo=modelo_por_defecto)

    uso = getattr(mensaje, "usage_metadata", None) or {}
    metadatos = getattr(mensaje, "response_metadata", None) or {}
    detalle = uso.get("input_token_details") or {}

    return Respuesta(
        texto=_texto_de(mensaje),
        modelo=(
            metadatos.get("model_name")
            or metadatos.get("model")
            or modelo_por_defecto
        ),
        tokens_entrada=uso.get("input_tokens", 0),
        tokens_salida=uso.get("output_tokens", 0),
        tokens_cache_leidos=detalle.get("cache_read", 0),
        tokens_cache_guardados=detalle.get("cache_creation", 0),
    )
