"""Pruebas del usuario y clave de la plataforma de pruebas en la nube.

No salen a internet ni gastan tokens: se arma una app de mentira con una ruta
común, una de streaming (como el chat) y el health check, y se le pone el
middleware adelante.

    pytest
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agente.web.acceso import AccesoBasico  # noqa: E402


def _basic(usuario: str, clave: str) -> dict:
    token = base64.b64encode(f"{usuario}:{clave}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def cliente() -> TestClient:
    async def inicio(request):
        return PlainTextResponse("adentro")

    async def salud(request):
        return PlainTextResponse("ok")

    async def chat(request):
        async def pedacitos():
            yield b"uno "
            yield b"dos"

        return StreamingResponse(pedacitos())

    app = Starlette(
        routes=[Route("/", inicio), Route("/salud", salud), Route("/chat", chat)]
    )
    app.add_middleware(AccesoBasico, usuario="leo", clave="clave-larga")
    return TestClient(app)


def test_sin_credenciales_pide_clave(cliente):
    r = cliente.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_con_la_clave_correcta_entra(cliente):
    r = cliente.get("/", headers=_basic("leo", "clave-larga"))
    assert r.status_code == 200
    assert r.text == "adentro"


@pytest.mark.parametrize(
    "usuario,clave",
    [("leo", "otra"), ("otro", "clave-larga"), ("", ""), ("leo", "clave-larga "), ("LEO", "clave-larga")],
)
def test_credenciales_incorrectas_no_entran(cliente, usuario, clave):
    assert cliente.get("/", headers=_basic(usuario, clave)).status_code == 401


def test_credencial_mal_formada_no_rompe_nada(cliente):
    # base64 inválido y esquema que no es Basic: tienen que dar 401, no 500.
    assert cliente.get("/", headers={"Authorization": "Basic ###"}).status_code == 401
    assert cliente.get("/", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_el_health_check_pasa_sin_clave(cliente):
    r = cliente.get("/salud")
    assert r.status_code == 200


def test_el_streaming_sigue_andando_detras_del_middleware(cliente):
    # El motivo de que sea un middleware ASGI puro: el chat es un stream.
    r = cliente.get("/chat", headers=_basic("leo", "clave-larga"))
    assert r.status_code == 200
    assert r.text == "uno dos"


def test_el_streaming_tambien_pide_clave(cliente):
    assert cliente.get("/chat").status_code == 401
