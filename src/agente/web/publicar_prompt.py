"""Publicar el prompt de verdad: lo commitea al repo del agente y redespliega.

El botón "Guardar" de al lado (ver app.py) guarda en este mismo contenedor,
para probar en el chat sin arriesgar nada. Este módulo es el que manda el
cambio al repositorio y dispara el redeploy del webhook real — recién ahí
el WhatsApp de verdad empieza a contestar con el prompt nuevo.

Solo funciona si están cargadas las variables GITHUB_TOKEN, GITHUB_REPO,
COOLIFY_URL, COOLIFY_TOKEN y COOLIFY_APP_UUID. Sin ellas (por ejemplo,
corriendo local con `python servidor.py`) el botón queda oculto: ver
`disponible()`.

Usa solo la librería estándar (urllib) a propósito: es una sola función que
habla con dos APIs por HTTP, no ameritaba sumar una dependencia nueva.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

_VARIABLES_REQUERIDAS = (
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "COOLIFY_URL",
    "COOLIFY_TOKEN",
    "COOLIFY_APP_UUID",
)


class ErrorDePublicacion(Exception):
    """El mensaje llega tal cual a la pantalla, como con los errores de proveedor."""


def disponible() -> bool:
    return all(os.getenv(v) for v in _VARIABLES_REQUERIDAS)


def _pedir(
    url: str,
    token: str,
    metodo: str = "GET",
    cuerpo: dict | None = None,
    headers_extra: dict | None = None,
) -> dict:
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    peticion = urllib.request.Request(url, data=datos, method=metodo)
    peticion.add_header("Authorization", f"Bearer {token}")
    peticion.add_header("Accept", "application/json")
    if datos is not None:
        peticion.add_header("Content-Type", "application/json")
    for clave, valor in (headers_extra or {}).items():
        peticion.add_header(clave, valor)

    try:
        with urllib.request.urlopen(peticion, timeout=20) as resp:
            crudo = resp.read().decode("utf-8")
            return json.loads(crudo) if crudo else {}
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", errors="replace")
        raise ErrorDePublicacion(f"{url} → HTTP {e.code}: {detalle[:300]}") from e
    except urllib.error.URLError as e:
        raise ErrorDePublicacion(f"{url} → {e.reason}") from e


def publicar(texto: str) -> dict:
    """Commitea `texto` como el nuevo prompt y dispara el redeploy del webhook.

    Devuelve el sha del commit y el uuid del deployment, para mostrarlos.
    """
    if not disponible():
        faltan = [v for v in _VARIABLES_REQUERIDAS if not os.getenv(v)]
        raise ErrorDePublicacion(
            "Falta configurar en las variables de entorno: " + ", ".join(faltan)
        )

    repo = os.environ["GITHUB_REPO"]
    rama = os.getenv("GITHUB_BRANCH", "main")
    ruta = os.getenv("GITHUB_PROMPT_PATH", "prompts/sistema.md")
    token_gh = os.environ["GITHUB_TOKEN"]

    url_contenido = f"https://api.github.com/repos/{repo}/contents/{ruta}?ref={rama}"
    actual = _pedir(url_contenido, token_gh)

    contenido_b64 = base64.b64encode((texto.strip() + "\n").encode("utf-8")).decode("ascii")
    commit = _pedir(
        f"https://api.github.com/repos/{repo}/contents/{ruta}",
        token_gh,
        metodo="PUT",
        cuerpo={
            "message": "Actualiza el prompt del sistema desde el panel",
            "content": contenido_b64,
            "sha": actual.get("sha"),
            "branch": rama,
        },
    )

    coolify_url = os.environ["COOLIFY_URL"].rstrip("/")
    respuesta = _pedir(
        f"{coolify_url}/mcp",
        os.environ["COOLIFY_TOKEN"],
        metodo="POST",
        cuerpo={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "deploy",
                "arguments": {"uuid": os.environ["COOLIFY_APP_UUID"], "force": False},
            },
        },
        headers_extra={"Accept": "application/json, text/event-stream"},
    )

    if "error" in respuesta:
        raise ErrorDePublicacion(f"Coolify rechazó el deploy: {respuesta['error']}")

    datos_deploy = json.loads(respuesta["result"]["content"][0]["text"]).get("data", {})

    return {
        "commit_sha": (commit.get("commit") or {}).get("sha"),
        "deployment_uuid": datos_deploy.get("deployment_uuid"),
    }
