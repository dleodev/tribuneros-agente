"""Pruebas de la web mayorista. No salen a internet.

Tiendanube se reemplaza por productos escritos a mano: lo que se prueba es lo
nuestro (qué entra al catálogo, cómo se ordenan los talles, qué pasa cuando
Tiendanube no contesta), no que la API de ellos ande.

    pytest
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import servidor  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FOTO = "https://acdn-us.mitiendanube.com/stores/1/products/boca-abc-1024-1024.png"


def producto(
    id=1,
    nombre="Boca Juniors - Titular 26/27 - Versión Jugador",
    publicado=True,
    talles=(("M", 2), ("S", 1), ("XL", 3)),
    categorias=("BOCA JUNIORS", "FÚTBOL ARGENTINO"),
    fotos=1,
) -> dict:
    return {
        "id": id,
        "name": {"es": nombre},
        "published": publicado,
        "categories": [{"name": {"es": c}} for c in categorias],
        "variants": [{"stock": s, "values": [{"es": t}]} for t, s in talles],
        "images": [{"position": i + 1, "src": FOTO.replace("abc", f"abc{i}")} for i in range(fotos)],
    }


@pytest.fixture(autouse=True)
def cache_limpia():
    servidor._cache.update(t=0.0, catalogo=None, imagenes={})
    servidor._fotos.clear()
    yield


@pytest.fixture
def cliente():
    return TestClient(servidor.app)


# -- Qué entra al catálogo ----------------------------------------------------


def test_entran_los_publicados_con_stock():
    catalogo, _ = servidor.armar_catalogo([producto()])

    assert [p["id"] for p in catalogo] == [1]


def test_no_entran_los_ocultos():
    """Si en la tienda no se ve, en el mayorista tampoco."""
    catalogo, _ = servidor.armar_catalogo([producto(publicado=False)])

    assert catalogo == []


def test_no_entra_lo_que_no_tiene_stock():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("M", 0), ("L", 0)))])

    assert catalogo == []


def test_un_talle_sin_stock_no_se_ofrece():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("M", 0), ("L", 2)))])

    assert [t["talle"] for t in catalogo[0]["talles"]] == ["L"]


def test_sin_control_de_stock_no_se_ofrece():
    """stock None = Tiendanube no lo cuenta: en mayorista hace falta saber cuántas hay."""
    p = producto()
    p["variants"][0]["stock"] = None

    catalogo, _ = servidor.armar_catalogo([p])

    assert "M" not in [t["talle"] for t in catalogo[0]["talles"]]


def test_sin_foto_no_entra():
    catalogo, _ = servidor.armar_catalogo([producto(fotos=0)])

    assert catalogo == []


def test_los_talles_salen_en_orden_de_talle_y_no_alfabetico():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("XL", 1), ("S", 1), ("L", 1), ("M", 1)))])

    assert [t["talle"] for t in catalogo[0]["talles"]] == ["S", "M", "L", "XL"]


def test_el_stock_es_el_de_tiendanube():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("M", 7),))])

    assert catalogo[0]["talles"] == [{"talle": "M", "stock": 7}]


def test_las_fotos_se_piden_chicas():
    _, imagenes = servidor.armar_catalogo([producto(fotos=2)])

    assert imagenes[1][0].endswith("-480-0.webp")
    assert len(imagenes[1]) == 2


# -- Nombre, versión y categorías ---------------------------------------------


@pytest.mark.parametrize(
    "crudo, esperado",
    [
        ("Boca Juniors - Titular 26/27 - Versión Jugador", "Boca Juniors - Titular 26/27"),
        ("Camiseta Argentina 2026 alternativa (Versión Jugador)", "Argentina 2026 alternativa"),
        ("Argentina Titular 2026 | Tela jugador - Escudos termosellados", "Argentina Titular 2026"),
        ("Manchester City - Alternativa 25/26 - Versión Jugador FODEN #47", "Manchester City - Alternativa 25/26"),
        ("River Plate - Titular 26/27 - Otamendi #30", "River Plate - Titular 26/27 - Otamendi #30"),
    ],
)
def test_el_nombre_se_limpia(crudo, esperado):
    assert servidor.nombre_limpio(crudo) == esperado


def test_la_version_jugador_se_detecta_por_el_nombre():
    assert servidor.version_de("Boca - Versión Jugador") == "jugador"
    assert servidor.version_de("Argentina Titular | Tela jugador") == "jugador"
    assert servidor.version_de("River Plate - Titular 26/27") == "fan"


def test_las_categorias_de_la_tienda_se_traducen():
    assert servidor.categorias_de(producto(categorias=("BOCA JUNIORS", "FÚTBOL ARGENTINO"))) == ["arg"]
    assert servidor.categorias_de(producto(categorias=("SELECCIÓN ARGENTINA", "RETRO / CLÁSICAS"))) == ["sel", "retro"]
    assert servidor.categorias_de(producto(categorias=("FÚTBOL INTERNACIONAL",))) == ["int"]


# -- La API -------------------------------------------------------------------


def test_la_api_devuelve_catalogo_y_reglas(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    r = cliente.get("/api/catalogo")

    assert r.status_code == 200
    d = r.json()
    assert d["minimo"] == 6
    assert d["tramos"] == [{"desde": 12, "precio": 35000}, {"desde": 6, "precio": 38000}]
    assert d["productos"][0]["nombre"] == "Boca Juniors - Titular 26/27"
    assert d["productos"][0]["version"] == "jugador"
    assert "no-store" in r.headers["cache-control"]


def test_el_catalogo_se_guarda_unos_minutos(cliente, monkeypatch):
    llamadas = []
    monkeypatch.setattr(servidor, "_traer_productos", lambda: llamadas.append(1) or [producto()])

    cliente.get("/api/catalogo")
    cliente.get("/api/catalogo")

    assert len(llamadas) == 1


def test_si_tiendanube_se_cae_se_muestra_el_ultimo_catalogo(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    cliente.get("/api/catalogo")
    servidor._cache["t"] = time.time() - servidor.CACHE_SEGUNDOS - 1  # venció

    def cae():
        raise OSError("sin conexión")

    monkeypatch.setattr(servidor, "_traer_productos", cae)
    r = cliente.get("/api/catalogo")

    assert r.status_code == 200
    assert len(r.json()["productos"]) == 1


def test_si_tiendanube_se_cae_y_no_hay_nada_avisa(cliente, monkeypatch):
    def cae():
        raise OSError("sin conexión")

    monkeypatch.setattr(servidor, "_traer_productos", cae)

    assert cliente.get("/api/catalogo").status_code == 503


def test_una_foto_que_no_existe_da_404(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    assert cliente.get("/img/999/1").status_code == 404
    assert cliente.get("/img/1/9").status_code == 404


def test_la_pagina_y_la_salud_responden(cliente):
    assert cliente.get("/salud").json() == {"estado": "ok"}
    r = cliente.get("/")
    assert r.status_code == 200
    assert "Tribuneros" in r.text


def test_la_pagina_dice_las_condiciones_de_compra(cliente):
    """Lo pidió el negocio: tiene que estar visible en la web."""
    texto = cliente.get("/").text

    assert "no quedan aseguradas" in texto
    assert "Andreani" in texto
    assert "a cargo del comprador" in texto
    assert "transferencia o efectivo" in texto
