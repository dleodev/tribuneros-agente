"""La memoria del agente.

Las APIs de los modelos NO recuerdan nada: cada llamada es independiente.
Que el agente "se acuerde" es puro trabajo nuestro — hay que volver a mandarle
la conversación entera en cada mensaje.

LangGraph resuelve eso con los checkpointers. Un checkpointer guarda el estado
de cada conversación (identificada por un thread_id) y lo vuelve a cargar solo.

    thread_id     = una conversación
    checkpointer  = dónde se guardan esas conversaciones

En el .env elegís con una variable:

    MODO=test        → SQLite, un archivo en tu computadora. Cero instalación.
    MODO=produccion  → Postgres. Para cuando hay varios procesos atendiendo.

Y hay una tercera, `ram()`, que no se guarda en ningún lado: sirve para los
tests y para ver el agente en su forma más simple.

El agente no cambia entre un modo y el otro. Cambia esta línea y nada más.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from .config import Config


def crear_memoria(config: "Config") -> "BaseCheckpointSaver":
    """Devuelve la memoria que corresponda al MODO del .env."""
    if config.modo == "produccion":
        if not config.postgres_dsn:
            raise ValueError(
                "MODO=produccion necesita POSTGRES_DSN en el .env.\n"
                "Ejemplo: POSTGRES_DSN=postgresql://usuario:clave@localhost:5432/agente"
            )
        return postgres(config.postgres_dsn)

    return sqlite(config.sqlite_ruta)


# ---------------------------------------------------------------------------


def ram() -> "BaseCheckpointSaver":
    """Memoria en RAM. Se borra al cerrar el programa.

    Es la forma más simple de ver cómo funciona: no guarda nada en ningún lado.
    La usan los tests.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def sqlite(ruta: str = "datos/conversaciones.db") -> "BaseCheckpointSaver":
    """Memoria en un archivo. Sobrevive al reinicio. → MODO=test

    Un archivo, cero servidores. Alcanza de sobra para desarrollar y para un
    bot chico de Telegram con un solo proceso atendiendo.
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    archivo = Path(ruta)
    archivo.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False porque el servidor web atiende en varios hilos.
    conexion = sqlite3.connect(archivo, check_same_thread=False)
    guardador = SqliteSaver(conexion)
    guardador.setup()
    return guardador


def postgres(dsn: str) -> "BaseCheckpointSaver":
    """Memoria en Postgres. → MODO=produccion

    Para cuando hay muchas conversaciones a la vez y más de un proceso
    respondiendo: es el caso de WhatsApp.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as e:
        # El motivo real va adentro del mensaje a propósito. Son dos fallas
        # distintas que se ven igual desde afuera: que falte el paquete, o que
        # esté puesto pero sin la libpq del sistema (el clásico "no pq wrapper
        # available" en Windows). Sin el detalle, la segunda te manda a
        # instalar algo que ya tenías.
        raise ImportError(
            f"MODO=produccion necesita el conector de Postgres.\n\n"
            f"    pip install \"langgraph-checkpoint-postgres>=3.1,<4\" \"psycopg[binary]\"\n\n"
            f"El error de abajo dice cuál de los dos falta:\n    {type(e).__name__}: {e}"
        ) from None

    # La conexión queda abierta mientras viva el proceso: si la cerráramos
    # acá, el checkpointer dejaría de funcionar en el primer mensaje.
    contexto = PostgresSaver.from_conn_string(dsn)
    guardador = contexto.__enter__()
    guardador.setup()

    # Y esta línea es la que hace que eso sea verdad. `from_conn_string` no es
    # un objeto cualquiera: es un generador (`with Connection.connect(...) as
    # conn: yield ...`). Si `contexto` se queda sin referencias al salir de
    # esta función, el recolector de basura lo destruye, y destruirlo ejecuta
    # el cierre del `with` — o sea, **cierra la conexión**. No falla acá:
    # falla más tarde, con un "the connection is closed" en el primer mensaje
    # que llega, cuando ya nadie se acuerda de esta línea.
    #
    # Guardándolo en el propio guardador, la conexión vive exactamente lo que
    # vive la memoria del agente.
    #
    # Ojo con probar esto en un script corto: si el proceso termina enseguida,
    # el recolector no llega a actuar y parece que anda igual. Se nota recién
    # cuando el agente queda corriendo un rato, como en el bot de Telegram.
    guardador._contexto_abierto = contexto

    return guardador
