"""Configuración del agente.

Todo sale del archivo .env. Nada de credenciales escritas en el código.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Raíz del proyecto (donde vive el .env)
RAIZ = Path(__file__).resolve().parents[2]

load_dotenv(RAIZ / ".env")


PROVEEDORES_VALIDOS = ("claude", "openai", "gemini")
MODOS_VALIDOS = ("test", "produccion")

# Qué variable de entorno lleva la clave de cada proveedor
CLAVE_POR_PROVEEDOR = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}

MODELOS_POR_DEFECTO = {
    "claude": "claude-opus-5",
    "openai": "gpt-5",
    "gemini": "gemini-2.5-pro",
}


class ErrorDeConfiguracion(Exception):
    """Falta algo en el .env o está mal puesto."""


@dataclass
class Config:
    proveedor: str
    modelo: str
    api_key: str
    max_tokens: int
    memoria_mensajes: int
    prompt_sistema: Path
    modo: str = "test"
    cache: bool = True
    sqlite_ruta: str = "datos/conversaciones.db"
    postgres_dsn: str = ""
    # Vacío mientras el agente corra solo en la computadora. Lo usa el bot
    # de Telegram; el resto del proyecto ni lo mira.
    telegram_token: str = ""

    # -- Chatwoot: solo lo mira el webhook (web/webhook.py) ------------------
    chatwoot_url: str = ""
    chatwoot_token: str = ""
    chatwoot_cuenta_id: str = "1"
    # La etiqueta que apaga al bot en una conversación: el traspaso a una
    # persona. Se pone con un clic desde la bandeja de Chatwoot.
    chatwoot_etiqueta_humano: str = "humano"
    # El secreto que va en la URL del webhook. Chatwoot no firma sus pedidos,
    # así que esto es lo único que separa un mensaje de verdad de cualquiera
    # que haya descubierto el dominio.
    chatwoot_webhook_token: str = ""
    # Cuánto espera juntando la ráfaga antes de contestar (ver buffer.py).
    buffer_segundos: int = 8

    @classmethod
    def desde_entorno(
        cls, proveedor: str | None = None, modelo: str | None = None
    ) -> "Config":
        """Arma la configuración leyendo el .env.

        Se le puede pasar un proveedor y un modelo a mano para pisar los del
        .env: así la plataforma de pruebas los cambia en caliente.
        """
        proveedor = (proveedor or os.getenv("PROVEEDOR", "claude")).strip().lower()

        if proveedor not in PROVEEDORES_VALIDOS:
            raise ErrorDeConfiguracion(
                f"El proveedor '{proveedor}' no existe. "
                f"Elegí uno de: {', '.join(PROVEEDORES_VALIDOS)}."
            )

        nombre_clave = CLAVE_POR_PROVEEDOR[proveedor]
        api_key = (os.getenv(nombre_clave) or "").strip()

        if not api_key:
            raise ErrorDeConfiguracion(
                f"Falta la clave de {proveedor}. "
                f"Abrí el archivo .env y completá {nombre_clave}."
            )

        modelo = (
            modelo
            or os.getenv(f"MODELO_{proveedor.upper()}")
            or MODELOS_POR_DEFECTO[proveedor]
        ).strip()

        modo = (os.getenv("MODO", "test") or "test").strip().lower()
        if modo not in MODOS_VALIDOS:
            raise ErrorDeConfiguracion(
                f"MODO tiene que ser 'test' o 'produccion', no '{modo}'."
            )

        return cls(
            proveedor=proveedor,
            modelo=modelo,
            api_key=api_key,
            max_tokens=_entero("MAX_TOKENS", 4096),
            memoria_mensajes=_entero("MEMORIA_MENSAJES", 20),
            prompt_sistema=RAIZ / os.getenv("PROMPT_SISTEMA", "prompts/sistema.md"),
            modo=modo,
            cache=_booleano("CACHE", True),
            sqlite_ruta=os.getenv("SQLITE_RUTA", "datos/conversaciones.db"),
            postgres_dsn=(os.getenv("POSTGRES_DSN") or "").strip(),
            telegram_token=(os.getenv("TELEGRAM_TOKEN") or "").strip(),
            chatwoot_url=(os.getenv("CHATWOOT_URL") or "").strip(),
            chatwoot_token=(os.getenv("CHATWOOT_TOKEN") or "").strip(),
            chatwoot_cuenta_id=(os.getenv("CHATWOOT_CUENTA_ID") or "1").strip(),
            chatwoot_etiqueta_humano=(
                os.getenv("CHATWOOT_ETIQUETA_HUMANO") or "humano"
            ).strip(),
            chatwoot_webhook_token=(
                os.getenv("CHATWOOT_WEBHOOK_TOKEN") or ""
            ).strip(),
            buffer_segundos=_entero("BUFFER_SEGUNDOS", 8),
        )


def proveedores_disponibles() -> dict[str, bool]:
    """Qué proveedores tienen la clave cargada. Lo usa la web para los botones."""
    return {
        nombre: bool((os.getenv(clave) or "").strip())
        for nombre, clave in CLAVE_POR_PROVEEDOR.items()
    }


# ---------------------------------------------------------------------------
# Guardar ajustes en el .env
# ---------------------------------------------------------------------------

# Solo estas variables se pueden tocar desde la plataforma de pruebas.
#
# NO están en la lista, a propósito:
#   · las claves de API  → no se editan desde el navegador
#   · MODO               → la plataforma es para probar: siempre test
#   · CACHE              → siempre activado; se apaga editando el .env a mano
AJUSTABLES = (
    "PROVEEDOR",
    "MODELO_CLAUDE",
    "MODELO_OPENAI",
    "MODELO_GEMINI",
    "MAX_TOKENS",
    "MEMORIA_MENSAJES",
)


def guardar_ajustes(cambios: dict[str, str]) -> None:
    """Escribe los cambios en el .env y los aplica sin reiniciar.

    Reemplaza solo la línea de cada variable y deja el resto del archivo
    intacto: los comentarios y el orden se conservan. Si la variable no
    estaba, la agrega al final.
    """
    archivo = RAIZ / ".env"

    permitidos = {
        clave: str(valor) for clave, valor in cambios.items() if clave in AJUSTABLES
    }
    if not permitidos:
        return

    lineas = (
        archivo.read_text(encoding="utf-8").splitlines()
        if archivo.exists()
        else []
    )

    pendientes = dict(permitidos)

    for i, linea in enumerate(lineas):
        pelada = linea.strip()
        if not pelada or pelada.startswith("#") or "=" not in pelada:
            continue

        nombre = pelada.split("=", 1)[0].strip()
        if nombre in pendientes:
            lineas[i] = f"{nombre}={pendientes.pop(nombre)}"

    for nombre, valor in pendientes.items():
        lineas.append(f"{nombre}={valor}")

    archivo.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    # Que el proceso que está corriendo vea los valores nuevos ya mismo.
    for nombre, valor in permitidos.items():
        os.environ[nombre] = valor


def clave_de(proveedor: str) -> str:
    """La clave de un proveedor, o cadena vacía si no está cargada."""
    variable = CLAVE_POR_PROVEEDOR.get(proveedor.strip().lower(), "")
    return (os.getenv(variable) or "").strip() if variable else ""


def _entero(nombre: str, por_defecto: int) -> int:
    valor = (os.getenv(nombre) or "").strip()
    if not valor:
        return por_defecto
    try:
        return int(valor)
    except ValueError:
        raise ErrorDeConfiguracion(
            f"{nombre} tiene que ser un número entero, no '{valor}'."
        ) from None


def _booleano(nombre: str, por_defecto: bool) -> bool:
    valor = (os.getenv(nombre) or "").strip().lower()
    if not valor:
        return por_defecto
    return valor in ("1", "true", "si", "sí", "on", "yes")
