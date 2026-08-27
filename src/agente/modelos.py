"""Los modelos: acá se elige con qué proveedor y con qué modelo habla el agente.

LangChain envuelve a todos los proveedores en la misma interfaz (BaseChatModel),
así que el resto del código no sabe ni le importa si adentro hay Claude,
OpenAI o Gemini. Cambiar de proveedor es cambiar una línea del .env.

Este archivo hace dos cosas:

    crear_modelo()   → arma el modelo con el que habla el agente
    listar_modelos() → le pregunta al proveedor qué modelos tiene hoy

Lo segundo existe para no dejar una lista escrita a mano que envejece: los
proveedores sacan modelos nuevos todo el tiempo y la plataforma de pruebas
los muestra apenas salen.

Los import van adentro de cada rama a propósito: así podés borrar del
requirements.txt los paquetes que no usás y el agente arranca igual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from langchain_core.language_models import BaseChatModel

PROVEEDORES = ("claude", "openai", "gemini")


# ---------------------------------------------------------------------------
# Crear el modelo
# ---------------------------------------------------------------------------


def crear_modelo(
    proveedor: str,
    api_key: str,
    modelo: str,
    max_tokens: int,
) -> "BaseChatModel":
    """Devuelve el modelo de LangChain que corresponda al proveedor."""
    proveedor = proveedor.strip().lower()
    max_tokens = limitar_max_tokens(proveedor, api_key, modelo, max_tokens)

    if proveedor == "claude":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError(
                "Falta el paquete de Claude: pip install langchain-anthropic"
            ) from None

        return ChatAnthropic(
            model=modelo,
            api_key=api_key,
            max_tokens=max_tokens,
            timeout=120,
            stop=None,
        )

    if proveedor == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError(
                "Falta el paquete de OpenAI: pip install langchain-openai"
            ) from None

        return ChatOpenAI(
            model=modelo,
            api_key=api_key,
            max_completion_tokens=max_tokens,
            timeout=120,
            # Sin esto, OpenAI no informa el consumo de tokens cuando
            # la respuesta llega en vivo.
            stream_usage=True,
            # OpenAI tiene dos endpoints y los modelos nuevos no hacen lo
            # mismo en los dos. Por el viejo (/v1/chat/completions), pedirle
            # herramientas a un modelo que razona —o sea, toda la familia
            # gpt-5— devuelve un 400:
            #
            #   "Function tools with reasoning_effort are not supported ...
            #    To use function tools, use /v1/responses"
            #
            # La otra salida que ofrece el error es apagarle el razonamiento
            # al modelo, que es pagar por un modelo y usar otro. Así que
            # vamos por el endpoint nuevo, que es además el que OpenAI
            # recomienda de acá en adelante. El streaming, el conteo de
            # tokens y el tope de salida siguen funcionando igual.
            use_responses_api=True,
        )

    if proveedor == "gemini":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ImportError(
                "Falta el paquete de Gemini: pip install langchain-google-genai"
            ) from None

        return ChatGoogleGenerativeAI(
            model=modelo,
            google_api_key=api_key,
            max_output_tokens=max_tokens,
            timeout=120,
        )

    raise ValueError(
        f"No conozco el proveedor '{proveedor}'. Usá: claude, openai o gemini."
    )


# ---------------------------------------------------------------------------
# Preguntarle al proveedor qué modelos tiene
# ---------------------------------------------------------------------------


def limitar_max_tokens(
    proveedor: str, api_key: str, modelo: str, max_tokens: int
) -> int:
    """Baja el máximo de respuesta si el modelo elegido no lo aguanta.

    Cada modelo tiene su propio techo y pedirle más devuelve un error 400 en
    la mitad de una conversación. Como el techo cambia de modelo en modelo, no
    lo tenemos escrito a mano: se lo preguntamos al proveedor (una sola vez,
    después queda guardado) y recortamos si hace falta.

    Si no se puede consultar, se manda el valor configurado tal cual.
    """
    tope = _tope_de(listar_modelos(proveedor, api_key), modelo)
    return min(max_tokens, tope) if tope else max_tokens


def _tope_de(modelos: list[dict], buscado: str) -> int | None:
    """Encuentra el tope de salida de un modelo dentro de la lista.

    No alcanza con comparar el nombre tal cual: los proveedores publican
    tanto el alias corto ("claude-haiku-4-5") como el que lleva la fecha
    ("claude-haiku-4-5-20251001"), y no siempre coinciden con el que pusiste
    en el .env. Si hay varios candidatos, nos quedamos con el tope más chico,
    que es el que no falla.
    """
    for m in modelos:
        if m["id"] == buscado:
            return m.get("max_salida")

    topes = [
        m["max_salida"]
        for m in modelos
        if m.get("max_salida")
        and (m["id"].startswith(f"{buscado}-") or buscado.startswith(f"{m['id']}-"))
    ]
    return min(topes) if topes else None


# La lista de modelos de un proveedor no cambia en el medio de una corrida:
# la pedimos una vez y la guardamos acá.
_recordados: dict[tuple[str, str], list[dict]] = {}


def listar_modelos(proveedor: str, api_key: str) -> list[dict]:
    """Devuelve los modelos de chat que el proveedor tiene disponibles hoy.

    Cada elemento es {"id", "nombre", "max_salida"}, del más nuevo al más viejo.

    `max_salida` es cuántos tokens puede escribir ese modelo como máximo en una
    respuesta. Cambia bastante entre modelos, y si le pedís más de lo que
    aguanta, la llamada falla. Por eso la plataforma lo usa para ajustar el
    tope sola cuando cambiás de modelo. Es None si el proveedor no lo informa.

    Si la consulta falla (sin internet, clave vencida), devuelve lista vacía:
    la plataforma sigue andando con lo que diga el .env.
    """
    proveedor = proveedor.strip().lower()
    guardado = _recordados.get((proveedor, api_key))
    if guardado is not None:
        return guardado

    try:
        if proveedor == "claude":
            modelos = _modelos_claude(api_key)
        elif proveedor == "openai":
            modelos = _modelos_openai(api_key)
        elif proveedor == "gemini":
            modelos = _modelos_gemini(api_key)
        else:
            return []
    except Exception:
        # Sin internet o clave vencida: no guardamos el resultado vacío,
        # así el próximo intento vuelve a probar.
        return []

    _recordados[(proveedor, api_key)] = modelos
    return modelos


def olvidar_modelos(proveedor: str | None = None) -> None:
    """Borra lo guardado para volver a preguntarle al proveedor."""
    if proveedor is None:
        _recordados.clear()
        return

    for clave in [c for c in _recordados if c[0] == proveedor]:
        _recordados.pop(clave, None)


def _modelos_claude(api_key: str) -> list[dict]:
    import anthropic

    cliente = anthropic.Anthropic(api_key=api_key)

    # La lista de Anthropic ya viene ordenada del más nuevo al más viejo
    # y trae solo modelos de chat: no hay nada que filtrar.
    return [
        {
            "id": m.id,
            "nombre": getattr(m, "display_name", None) or m.id,
            "max_salida": getattr(m, "max_tokens", None),
        }
        for m in cliente.models.list(limit=60)
    ]


def _modelos_openai(api_key: str) -> list[dict]:
    from openai import OpenAI

    cliente = OpenAI(api_key=api_key)

    # OpenAI devuelve TODO junto: imágenes, audio, embeddings, modelos viejos.
    # Nos quedamos solo con los de chat y los ordenamos por fecha de salida,
    # así el más nuevo queda arriba de la lista.
    modelos = [m for m in cliente.models.list() if _es_chat_de_openai(m.id)]
    modelos.sort(key=lambda m: getattr(m, "created", 0), reverse=True)

    # OpenAI no informa el tope de salida en su listado: queda en None y la
    # plataforma usa el valor que tengas puesto.
    return [{"id": m.id, "nombre": m.id, "max_salida": None} for m in modelos]


def _es_chat_de_openai(identificador: str) -> bool:
    familias = ("gpt-", "o1", "o3", "o4", "chatgpt-")
    descartar = (
        "audio", "realtime", "transcribe", "tts", "whisper", "image",
        "embedding", "moderation", "search", "instruct", "dall-e",
        "vision-preview", "codex",
    )
    ident = identificador.lower()
    return ident.startswith(familias) and not any(p in ident for p in descartar)


def _modelos_gemini(api_key: str) -> list[dict]:
    from google import genai

    cliente = genai.Client(api_key=api_key)

    modelos = []
    for m in cliente.models.list():
        acciones = getattr(m, "supported_actions", None) or []
        if "generateContent" not in acciones:
            continue

        # Los nombres vienen como "models/gemini-2.5-pro": sacamos el prefijo.
        ident = (m.name or "").removeprefix("models/")
        if not ident or "embedding" in ident:
            continue

        modelos.append(
            {
                "id": ident,
                "nombre": getattr(m, "display_name", None) or ident,
                "max_salida": getattr(m, "output_token_limit", None),
            }
        )

    modelos.sort(key=lambda m: m["id"], reverse=True)
    return modelos
