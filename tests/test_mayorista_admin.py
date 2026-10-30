"""Pruebas del panel de administración del mayorista. No salen a internet.

Tiendanube se reemplaza por una tienda de mentira en memoria: lo que se prueba
es lo nuestro. Lo más importante de este archivo:

  · `test_un_conflicto_no_aplica_ni_los_cambios_buenos`: si entra una venta
    mientras alguien edita la planilla, no se pisa el stock.
  · Todo lo que no es el login exige sesión.

    pytest
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import acceso  # noqa: E402
import admin  # noqa: E402
import historial as hist  # noqa: E402
import tn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLAVE = "clave-de-prueba-123"
H = {"x-mayorista": "1"}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 50).decode()


class TiendaFalsa:
    """Una tienda en memoria con la forma que devuelve la API de Tiendanube."""

    def __init__(self):
        self.productos = {
            1: self._producto(1, "Boca Juniors - Titular 26/27", True, {"S": 0, "M": 2, "L": 3}),
            2: self._producto(2, "Argentina Retro", False, {"XL": 0}),  # oculto y sin stock
        }
        self.creados = []
        self._siguiente = 100

    @staticmethod
    def _producto(id, nombre, publicado, talles):
        return {
            "id": id,
            "name": {"es": nombre},
            "published": publicado,
            "categories": [{"name": {"es": "FÚTBOL ARGENTINO"}}],
            "images": [{"position": 1, "src": "https://acdn-us.mitiendanube.com/x/a-1024-1024.png"}],
            "variants": [
                {"id": id * 10 + i, "price": "75000.00", "stock": s, "stock_management": True,
                 "values": [{"es": t}], "inventory_levels": [{"location_id": "LOC", "stock": s}]}
                for i, (t, s) in enumerate(talles.items())
            ],
        }

    def stock(self, producto, talle):
        return next(v["stock"] for v in self.productos[producto]["variants"] if v["values"][0]["es"] == talle)

    def vender(self, producto, talle, n=1):
        for v in self.productos[producto]["variants"]:
            if v["values"][0]["es"] == talle:
                v["stock"] -= n

    def instalar(self, monkeypatch):
        monkeypatch.setattr(tn, "TIENDA", "1")
        monkeypatch.setattr(tn, "TOKEN", "token")
        monkeypatch.setattr(tn, "todos_los_productos", lambda: list(self.productos.values()))
        monkeypatch.setattr(tn, "productos_por_id", lambda ids: {i: self.productos[i] for i in ids if i in self.productos})
        monkeypatch.setattr(tn, "categorias", lambda: [{"id": 5, "name": {"es": "FÚTBOL ARGENTINO"}, "parent": None}, {"id": 6, "name": {"es": "BOCA"}, "parent": 5}])
        monkeypatch.setattr(tn, "actualizar_stock", self._actualizar)
        monkeypatch.setattr(tn, "crear_variante", self._crear_variante)
        monkeypatch.setattr(tn, "crear_producto", self._crear_producto)

    def _actualizar(self, cambios):
        for producto, variante, stock, _ in cambios:
            for v in self.productos[producto]["variants"]:
                if v["id"] == variante:
                    v["stock"] = stock

    def _crear_variante(self, producto, talle, stock, precio):
        self._siguiente += 1
        self.productos[producto]["variants"].append(
            {"id": self._siguiente, "price": precio, "stock": stock, "stock_management": True, "values": [{"es": talle}]}
        )
        return {}

    def _crear_producto(self, **datos):
        self._siguiente += 1
        self.creados.append(datos)
        return {"id": self._siguiente}


@pytest.fixture
def tienda(monkeypatch):
    t = TiendaFalsa()
    t.instalar(monkeypatch)
    return t


@pytest.fixture
def memoria():
    return hist.HistorialMemoria()


@pytest.fixture
def invalidaciones():
    return []


@pytest.fixture
def cliente(monkeypatch, tienda, memoria, invalidaciones):
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    acceso._fallos.clear()
    app = FastAPI()
    app.include_router(admin.crear_router(memoria, lambda: invalidaciones.append(1)))
    return TestClient(app)


@pytest.fixture
def sesion(cliente):
    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)
    assert r.status_code == 200
    return cliente


def v(tienda, producto, talle):
    return next(x for x in tienda.productos[producto]["variants"] if x["values"][0]["es"] == talle)


def cambio(tienda, producto, talle, despues, antes=None):
    var = v(tienda, producto, talle)
    return {"tipo": "stock", "producto": producto, "variante": var["id"], "antes": var["stock"] if antes is None else antes, "despues": despues}


# -- Entrar -------------------------------------------------------------------


@pytest.mark.parametrize("metodo, ruta", [("get", "/admin/api/stock"), ("get", "/admin/api/historial"), ("get", "/admin/api/categorias"), ("post", "/admin/api/stock/aplicar"), ("post", "/admin/api/productos")])
def test_sin_sesion_no_se_entra_a_nada(cliente, metodo, ruta):
    r = getattr(cliente, metodo)(ruta, headers=H, **({"json": {}} if metodo == "post" else {}))

    assert r.status_code == 401


def test_con_la_clave_correcta_se_entra(cliente):
    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert cliente.get("/admin/api/estado").json() == {"configurado": True, "usuario": "leo"}


@pytest.mark.parametrize("usuario, clave", [("leo", "mala"), ("otro", CLAVE), ("", ""), ("LEO", CLAVE)])
def test_con_datos_equivocados_no_se_entra(cliente, usuario, clave):
    r = cliente.post("/admin/api/login", json={"usuario": usuario, "clave": clave}, headers=H)

    assert r.status_code == 401
    assert cliente.get("/admin/api/estado").json()["usuario"] is None


def test_tras_cinco_fallos_se_bloquea_aunque_la_clave_sea_buena(cliente):
    for _ in range(5):
        cliente.post("/admin/api/login", json={"usuario": "leo", "clave": "mala"}, headers=H)

    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert r.status_code == 429


def test_el_bloqueo_se_levanta_con_el_tiempo():
    acceso._fallos.clear()
    for _ in range(5):
        acceso.anotar_fallo("1.2.3.4", ahora=1000.0)

    assert acceso.bloqueado("1.2.3.4", ahora=1100.0)
    assert not acceso.bloqueado("1.2.3.4", ahora=1000.0 + acceso.VENTANA_SEGUNDOS + 1)


def test_salir_borra_la_sesion(sesion):
    sesion.post("/admin/api/logout", headers=H)

    assert sesion.get("/admin/api/stock").status_code == 401


def test_una_cookie_adulterada_no_sirve(cliente):
    valida = acceso.crear_sesion("leo")
    cuerpo, firma = valida.rsplit(".", 1)

    assert acceso.leer_sesion(valida) == "leo"
    assert acceso.leer_sesion(cuerpo + "." + "0" * len(firma)) is None
    assert acceso.leer_sesion("basura") is None
    assert acceso.leer_sesion(None) is None


def test_una_sesion_vencida_no_sirve():
    vieja = acceso.crear_sesion("leo", ahora=time.time() - acceso.DURACION_SEGUNDOS - 10)

    assert acceso.leer_sesion(vieja) is None


def test_un_secreto_corto_apaga_el_panel(cliente, monkeypatch):
    """Sin un secreto largo las cookies se podrían falsificar: mejor no tener panel."""
    monkeypatch.setattr(acceso, "SECRETO", "corto")

    assert cliente.get("/admin/api/estado").json()["configurado"] is False
    assert cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 503


def test_los_cambios_exigen_el_header_propio(sesion, tienda):
    """Es lo que impide que otra web mande cambios desde tu navegador."""
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 5)]})

    assert r.status_code == 403
    assert tienda.stock(1, "M") == 2


# -- Ver ----------------------------------------------------------------------


def test_la_planilla_trae_todos_los_modelos_tambien_los_ocultos_y_sin_stock(sesion):
    r = sesion.get("/admin/api/stock")

    productos = {p["id"]: p for p in r.json()["productos"]}
    assert set(productos) == {1, 2}
    assert productos[2]["publicado"] is False
    assert [x["talle"] for x in productos[1]["variantes"]] == ["S", "M", "L"]


def test_las_categorias_traen_el_camino_completo(sesion):
    nombres = [c["nombre"] for c in sesion.get("/admin/api/categorias").json()]

    assert "FÚTBOL ARGENTINO › BOCA" in nombres


# -- Guardar ------------------------------------------------------------------


def test_guardar_cambia_el_stock_y_lo_anota(sesion, tienda, memoria, invalidaciones):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 7), cambio(tienda, 1, "S", 4)]}, headers=H)

    assert r.status_code == 200
    assert r.json()["aplicados"] == 2
    assert (tienda.stock(1, "M"), tienda.stock(1, "S")) == (7, 4)
    assert {(e["talle"], e["antes"], e["despues"]) for e in memoria.filas} == {("M", 2, 7), ("S", 0, 4)}
    assert memoria.filas[0]["usuario"] == "leo"
    assert invalidaciones, "la web pública tiene que volver a leer el stock"


def test_un_conflicto_no_aplica_ni_los_cambios_buenos(sesion, tienda, memoria):
    """Alguien compró una M mientras editabas: no se pisa, y no se guarda nada."""
    mios = [cambio(tienda, 1, "M", 9), cambio(tienda, 1, "L", 8)]
    tienda.vender(1, "M")  # entra una venta en el medio

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": mios}, headers=H)

    assert r.status_code == 409
    conflicto = r.json()["detail"]["conflictos"][0]
    assert (conflicto["talle"], conflicto["esperado"], conflicto["actual"]) == ("M", 2, 1)
    assert tienda.stock(1, "L") == 3, "el cambio bueno tampoco se aplicó"
    assert memoria.filas == []


def test_no_se_guarda_stock_negativo(sesion, tienda):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", -1)]}, headers=H)

    assert r.status_code == 422
    assert tienda.stock(1, "M") == 2


def test_guardar_sin_cambios_reales_no_hace_nada(sesion, tienda, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 2)]}, headers=H)

    assert r.status_code == 200
    assert r.json()["lote"] is None
    assert memoria.filas == []


def test_un_talle_nuevo_se_crea_con_el_precio_del_modelo(sesion, tienda, memoria):
    r = sesion.post(
        "/admin/api/stock/aplicar",
        json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": "XXL", "despues": 3}]},
        headers=H,
    )

    assert r.status_code == 200
    assert r.json()["creados"] == 1
    nuevo = v(tienda, 1, "XXL")
    assert (nuevo["stock"], nuevo["price"]) == (3, "75000.00")
    assert memoria.filas[0]["accion"] == "talle_nuevo"


def test_un_talle_que_ya_existe_no_se_duplica(sesion, tienda):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": " m ", "despues": 3}]}, headers=H)

    assert r.status_code == 409


def test_en_bloques_de_cincuenta(monkeypatch):
    """Tiendanube acepta hasta 50 variantes por pedido: más se parte."""
    pedidos = []
    monkeypatch.setattr(tn, "pedir", lambda metodo, ruta, cuerpo=None, **k: pedidos.append(cuerpo))

    tn.actualizar_stock([(1, i, 5, None) for i in range(120)])

    assert [sum(len(p["variants"]) for p in pedido) for pedido in pedidos] == [50, 50, 20]


def test_el_pedido_de_stock_agrupa_por_modelo_y_lleva_la_ubicacion():
    lote = tn.armar_lote_de_stock([(1, 11, 5, "LOC"), (1, 12, 0, "LOC"), (2, 21, 3, None)])

    assert lote == [
        {"id": 1, "variants": [{"id": 11, "inventory_levels": [{"stock": 5, "location_id": "LOC"}]}, {"id": 12, "inventory_levels": [{"stock": 0, "location_id": "LOC"}]}]},
        {"id": 2, "variants": [{"id": 21, "inventory_levels": [{"stock": 3}]}]},
    ]


# -- Deshacer -----------------------------------------------------------------


def test_deshacer_devuelve_el_stock_de_antes(sesion, tienda, memoria):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 7), cambio(tienda, 1, "L", 9)]}, headers=H).json()["lote"]

    r = sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert r.status_code == 200
    assert (tienda.stock(1, "M"), tienda.stock(1, "L")) == (2, 3)


def test_no_se_deshace_dos_veces(sesion, tienda):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 7)]}, headers=H).json()["lote"]
    sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H).status_code == 404


def test_si_el_stock_cambio_despues_no_se_deshace(sesion, tienda):
    """Deshacer un guardado de ayer no puede pisar las ventas de hoy."""
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 7)]}, headers=H).json()["lote"]
    tienda.vender(1, "M")

    r = sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert r.status_code == 409
    assert tienda.stock(1, "M") == 6


def test_deshacer_un_talle_nuevo_lo_deja_en_cero(sesion, tienda):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": "XXL", "despues": 3}]}, headers=H).json()["lote"]

    sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert tienda.stock(1, "XXL") == 0


def test_el_historial_agrupa_por_guardado(sesion, tienda):
    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(tienda, 1, "M", 7), cambio(tienda, 1, "L", 9)]}, headers=H)

    lotes = sesion.get("/admin/api/historial").json()

    assert len(lotes) == 1 and len(lotes[0]["entradas"]) == 2
    assert lotes[0]["se_puede_deshacer"] is True


# -- Modelos nuevos -----------------------------------------------------------


def modelo(**extra):
    return {"nombre": "Boca 2004 Retro", "precio": 75000, "talles": {"S": 1, "M": 2}, "categorias": [5], "fotos": [{"nombre": "frente.png", "datos": PNG}], **extra}


def test_se_crea_un_modelo_nuevo_oculto_por_defecto(sesion, tienda, memoria):
    r = sesion.post("/admin/api/productos", json=modelo(), headers=H)

    assert r.status_code == 200
    creado = tienda.creados[0]
    assert creado["publicado"] is False
    assert creado["talles"] == {"S": 1, "M": 2}
    assert creado["precio"] == "75000.00"
    assert creado["fotos"][0]["filename"] == "frente.png"
    assert memoria.filas[0]["accion"] == "producto_nuevo"


def test_un_modelo_sin_talles_no_se_crea(sesion, tienda):
    r = sesion.post("/admin/api/productos", json=modelo(talles={}), headers=H)

    assert r.status_code == 422
    assert tienda.creados == []


def test_una_foto_que_no_es_imagen_no_se_acepta(sesion, tienda):
    texto = base64.b64encode(b"esto no es una imagen").decode()

    r = sesion.post("/admin/api/productos", json=modelo(fotos=[{"nombre": "x.png", "datos": texto}]), headers=H)

    assert r.status_code == 422
    assert tienda.creados == []


def test_una_foto_demasiado_pesada_no_se_acepta(sesion, tienda):
    grande = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (admin.PESO_MAXIMO_DE_FOTO + 1)).decode()

    r = sesion.post("/admin/api/productos", json=modelo(fotos=[{"nombre": "x.png", "datos": grande}]), headers=H)

    assert r.status_code == 422


def test_el_nombre_de_la_foto_no_puede_llevar_rutas():
    foto = admin.decodificar_foto(admin.Foto(nombre="../../etc/passwd.png", datos=PNG))

    assert "/" not in foto["filename"] and "\\" not in foto["filename"]


# -- La página del panel -------------------------------------------------------


def test_la_pagina_del_panel_se_sirve_y_no_se_indexa():
    sys.modules.pop("sitio", None)
    import sitio as servidor

    c = TestClient(servidor.app)
    r = c.get("/admin/")

    assert r.status_code == 200
    assert "Panel mayorista" in r.text
    assert "noindex" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


def test_sin_configurar_el_panel_responde_503_y_no_deja_pasar():
    sys.modules.pop("sitio", None)
    import sitio as servidor

    c = TestClient(servidor.app)

    assert c.get("/admin/api/estado").json()["configurado"] is False
    assert c.get("/admin/api/stock").status_code == 503
