"""La plataforma de pruebas: una web local para hablar con el agente.

No es parte del agente. Es un banco de pruebas para verlo funcionar,
cambiar de modelo en caliente y editar el prompt sin reiniciar nada.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..agente import Agente
from ..config import (
    RAIZ,
    Config,
    ErrorDeConfiguracion,
    clave_de,
    guardar_ajustes,
    proveedores_disponibles,
)
from ..modelos import listar_modelos, olvidar_modelos
from ..prompts import guardar_prompt, leer_prompt

ESTATICOS = Path(__file__).parent / "static"

app = FastAPI(title="AgentKit — plataforma de pruebas")

# Un agente vivo por vez. La conversación se identifica con un thread_id
# (acá siempre "web"); en Telegram o WhatsApp sería el chat de cada persona.
CONVERSACION = "web"

_agente: Agente | None = None
_proveedor_activo: str | None = None
_modelo_activo: str | None = None


def obtener_agente(proveedor: str | None = None, modelo: str | None = None) -> Agente:
    """Devuelve el agente. Si cambió el proveedor o el modelo, lo recrea."""
    global _agente, _proveedor_activo, _modelo_activo

    cambio = (proveedor and proveedor != _proveedor_activo) or (
        modelo and modelo != _modelo_activo
    )

    if _agente is None or cambio:
        config = Config.desde_entorno(proveedor, modelo)
        # La memoria se reusa: cambiar de modelo no borra la conversación.
        anterior = _agente.checkpointer if _agente else None
        _agente = Agente(config, checkpointer=anterior)
        _proveedor_activo = config.proveedor
        _modelo_activo = config.modelo

    return _agente


# -- Modelos de entrada -------------------------------------------------------


class PeticionDeMensaje(BaseModel):
    texto: str
    proveedor: str | None = None
    modelo: str | None = None


class PromptEntrante(BaseModel):
    texto: str


class AjustesEntrantes(BaseModel):
    """Lo que la plataforma puede cambiar.

    El modo (siempre test) y el caché (siempre activado) no están acá: se
    cambian editando el .env a mano. El máximo de respuesta tampoco lo elige
    el usuario — lo manda la plataforma según lo que aguanta cada modelo.
    """

    proveedor: str | None = None
    modelo: str | None = None
    max_tokens: int | None = None
    memoria_mensajes: int | None = None


# -- Rutas --------------------------------------------------------------------


@app.get("/")
def inicio() -> FileResponse:
    return FileResponse(ESTATICOS / "index.html")


@app.get("/api/estado")
def estado() -> dict:
    """Qué proveedores están configurados y con qué está andando el agente."""
    disponibles = proveedores_disponibles()

    try:
        config = Config.desde_entorno(_proveedor_activo, _modelo_activo)
        activo = {
            "proveedor": config.proveedor,
            "modelo": config.modelo,
            "max_tokens": config.max_tokens,
            "memoria_mensajes": config.memoria_mensajes,
            "modo": config.modo,
            "cache": config.cache,
            "memoria": "Postgres" if config.modo == "produccion" else "SQLite",
        }
        error = None
    except ErrorDeConfiguracion as e:
        activo = None
        error = str(e)

    return {
        "disponibles": disponibles,
        "hay_alguno": any(disponibles.values()),
        "activo": activo,
        "error": error,
        "mensajes_en_memoria": _mensajes_en_memoria(),
    }


@app.get("/api/modelos")
def modelos(proveedor: str, refrescar: bool = False) -> dict:
    """Los modelos que ese proveedor tiene disponibles hoy, según su propia API."""
    if refrescar:
        olvidar_modelos(proveedor)

    clave = clave_de(proveedor)
    return {
        "proveedor": proveedor,
        "modelos": listar_modelos(proveedor, clave) if clave else [],
    }


@app.get("/api/prompt")
def obtener_prompt() -> dict:
    ruta = _ruta_prompt()
    return {"texto": leer_prompt(ruta), "ruta": str(ruta)}


@app.post("/api/prompt")
def escribir_prompt(entrada: PromptEntrante) -> dict:
    ruta = _ruta_prompt()
    guardar_prompt(ruta, entrada.texto)
    # No hace falta reiniciar nada: el prompt se relee en cada mensaje.
    return {"ok": True, "ruta": str(ruta)}


@app.post("/api/ajustes")
def ajustes(entrada: AjustesEntrantes) -> dict:
    """Guarda los ajustes en el .env y los aplica sin reiniciar el servidor."""
    global _agente, _proveedor_activo, _modelo_activo

    cambios: dict[str, str] = {}

    if entrada.proveedor:
        cambios["PROVEEDOR"] = entrada.proveedor
    if entrada.modelo and entrada.proveedor:
        cambios[f"MODELO_{entrada.proveedor.upper()}"] = entrada.modelo
    if entrada.max_tokens:
        cambios["MAX_TOKENS"] = str(entrada.max_tokens)
    if entrada.memoria_mensajes:
        cambios["MEMORIA_MENSAJES"] = str(entrada.memoria_mensajes)

    try:
        guardar_ajustes(cambios)
    except OSError as e:
        return {"ok": False, "error": f"No se pudo escribir el .env: {e}"}

    # El agente se rearma con la configuración nueva, pero reusando la memoria:
    # cambiar de modelo o de proveedor no borra la conversación.
    anterior = _agente.checkpointer if _agente else None

    _agente = None
    _proveedor_activo = entrada.proveedor or _proveedor_activo
    _modelo_activo = entrada.modelo or _modelo_activo

    try:
        config = Config.desde_entorno(_proveedor_activo, _modelo_activo)
        _agente = Agente(config, checkpointer=anterior)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {"ok": True, "guardado": sorted(cambios)}


@app.post("/api/reiniciar")
def reiniciar() -> dict:
    """Borra la conversación de verdad.

    Ojo: no alcanza con tirar el agente y crear otro. La conversación no vive
    en el agente, vive en el checkpointer: si no la borrás de ahí, el agente
    nuevo la vuelve a levantar y el botón parece andar pero no hizo nada.
    """
    if _agente is not None:
        _agente.olvidar(CONVERSACION)

    return {"ok": True, "mensajes_en_memoria": _mensajes_en_memoria()}


@app.post("/api/mensaje")
def mensaje(entrada: PeticionDeMensaje) -> StreamingResponse:
    """Manda un mensaje y devuelve la respuesta en vivo, de a pedacitos."""

    def eventos():
        try:
            agente = obtener_agente(entrada.proveedor, entrada.modelo)
        except (ErrorDeConfiguracion, ImportError, ValueError) as e:
            yield _evento("error", {"mensaje": str(e)})
            return

        try:
            transmision = agente.responder_en_vivo(entrada.texto, CONVERSACION)

            for pedazo in transmision:
                yield _evento("texto", {"texto": pedazo})

            final = transmision.resumen
            yield _evento(
                "fin",
                {
                    "modelo": final.modelo if final else agente.config.modelo,
                    "tokens_entrada": final.tokens_entrada if final else 0,
                    "tokens_salida": final.tokens_salida if final else 0,
                    "cache_leidos": final.tokens_cache_leidos if final else 0,
                    "cache_guardados": final.tokens_cache_guardados if final else 0,
                    "mensajes_en_memoria": _mensajes_en_memoria(),
                },
            )
        except Exception as e:  # el error del proveedor tiene que llegar a la pantalla
            yield _evento("error", {"mensaje": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        eventos(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- Ayudantes ----------------------------------------------------------------


def _evento(tipo: str, datos: dict) -> str:
    """Formato Server-Sent Events: así el navegador recibe el texto de a poco."""
    return f"event: {tipo}\ndata: {json.dumps(datos, ensure_ascii=False)}\n\n"


def _ruta_prompt() -> Path:
    return RAIZ / os.getenv("PROMPT_SISTEMA", "prompts/sistema.md")


def _mensajes_en_memoria() -> int:
    if _agente is None:
        return 0
    try:
        return len(_agente.historial(CONVERSACION))
    except Exception:
        return 0
