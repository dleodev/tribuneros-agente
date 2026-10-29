"""La web mayorista de Tribuneros (mayorista.tribuneros.store).

    python servidor.py

Es un catálogo con carrito que manda el pedido por WhatsApp. El stock y los
talles NO se copian a mano: se leen de Tiendanube cada pocos minutos, así que
lo que se ve acá es lo mismo que tiene la tienda.

Qué hace este servidor, en tres cosas:

  · GET /api/catalogo   los productos publicados que tienen stock, con sus
                        talles (los mismos de Tiendanube) y cuántas unidades
                        quedan de cada uno.
  · GET /img/<id>/<n>   la foto del producto. Pasa por acá porque la CDN de
                        Tiendanube rechaza los pedidos que no parecen de un
                        navegador, y así además se guarda en memoria.
  · GET /               la página (static/index.html).

No toma pedidos ni descuenta stock: el pedido sale por WhatsApp y el stock se
descuenta en Tiendanube cuando llega el pago (ver las condiciones en la web).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("mayorista")

CARPETA = Path(__file__).parent
PUERTO = int(os.getenv("PUERTO", "8000"))

TIENDA = os.getenv("TIENDANUBE_STORE_ID", "").strip()
TOKEN = os.getenv("TIENDANUBE_ACCESS_TOKEN", "").strip()
CACHE_SEGUNDOS = int(os.getenv("MAYORISTA_CACHE_SEGUNDOS", "300"))
# Con "true" no se puede pedir más unidades de un talle que las que hay.
LIMITAR_STOCK = os.getenv("MAYORISTA_LIMITAR_STOCK", "true").strip().lower() in ("1", "true", "si", "yes")
WHATSAPP = os.getenv("MAYORISTA_WHATSAPP", "5491178173282").strip()

# Precio por unidad según el total de camisetas del pedido. Del más alto al más bajo.
TRAMOS = [{"desde": 12, "precio": 35000}, {"desde": 6, "precio": 38000}]
MINIMO = 6

ORDEN_DE_TALLES = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
CABECERAS_CDN = {
    # La CDN devuelve 403 si el pedido no parece de un navegador.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": "https://tribuneros.store/",
    "Accept": "image/webp,image/*",
}

_LIMPIAR = [
    r"\s*\|.*$",
    r"\s*[-–]\s*Versi[oó]n (Jugador|Hincha)(\s+FODEN.*)?",
    r"\s*\(Versi[oó]n Jugador\)",
    r"\s*[-–]\s*Con Parche F[uú]tbol Argentino",
    r"^Camiseta\s+",
]

_candado = threading.Lock()
_cache: dict = {"t": 0.0, "catalogo": None, "imagenes": {}}
_fotos: dict[str, bytes] = {}


# -- Tiendanube ---------------------------------------------------------------


def _pedir(ruta: str) -> list | dict:
    pedido = urllib.request.Request(
        f"https://api.tiendanube.com/v1/{TIENDA}{ruta}",
        headers={
            # Tiendanube no usa "Authorization": su header se llama Authentication.
            "Authentication": f"bearer {TOKEN}",
            "User-Agent": "Tribuneros Mayorista (hello@levelone.agency)",
        },
    )
    with urllib.request.urlopen(pedido, timeout=30) as respuesta:
        return json.load(respuesta)


def _traer_productos() -> list[dict]:
    if not TIENDA or not TOKEN:
        raise RuntimeError("Faltan TIENDANUBE_STORE_ID y TIENDANUBE_ACCESS_TOKEN.")
    productos: list[dict] = []
    for pagina in range(1, 21):
        lote = _pedir(f"/products?per_page=200&page={pagina}")
        productos += lote
        if len(lote) < 200:
            break
    return productos


# -- Armar el catálogo --------------------------------------------------------


def nombre_limpio(nombre: str) -> str:
    """Saca del nombre lo que la web muestra aparte (la versión, los apéndices)."""
    for patron in _LIMPIAR:
        nombre = re.sub(patron, "", nombre, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", nombre).strip(" -–")


def categorias_de(producto: dict) -> list[str]:
    nombres = {(c.get("name") or {}).get("es") for c in producto.get("categories") or []}
    salida = []
    if nombres & {"FÚTBOL ARGENTINO", "BOCA JUNIORS", "RIVER PLATE"}:
        salida.append("arg")
    if "SELECCIÓN ARGENTINA" in nombres:
        salida.append("sel")
    if "FÚTBOL INTERNACIONAL" in nombres:
        salida.append("int")
    if "RETRO / CLÁSICAS" in nombres:
        salida.append("retro")
    return salida


def version_de(nombre: str) -> str:
    """"jugador" si el nombre lo dice; si no, "fan"."""
    return "jugador" if re.search(r"versi[oó]n jugador|tela jugador", nombre, re.IGNORECASE) else "fan"


def _clave_de_talle(talle: str) -> tuple[int, str]:
    base = talle.split(" - ")[0].strip().upper()
    return (ORDEN_DE_TALLES.index(base) if base in ORDEN_DE_TALLES else 99, talle)


def armar_catalogo(productos: list[dict]) -> tuple[list[dict], dict[int, list[str]]]:
    """Los productos publicados con stock. Devuelve (catálogo, urls de fotos)."""
    catalogo: list[dict] = []
    imagenes: dict[int, list[str]] = {}

    for p in productos:
        if not p.get("published"):
            continue

        talles = []
        for v in p.get("variants") or []:
            stock = v.get("stock")
            # stock None = la variante no lleva control de stock: no se ofrece
            # (en mayorista hace falta saber cuántas hay).
            if stock is None or stock <= 0:
                continue
            etiqueta = ", ".join(x.get("es", "") for x in v.get("values") or [] if x.get("es")) or "Único"
            talles.append({"talle": etiqueta, "stock": int(stock)})
        if not talles:
            continue
        talles.sort(key=lambda t: _clave_de_talle(t["talle"]))

        fotos = []
        for imagen in sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)[:2]:
            src = imagen.get("src") or ""
            if src:
                # 480 px alcanza para la grilla y pesa unos 40 KB.
                fotos.append(re.sub(r"-\d+-\d+\.(png|jpe?g|webp)$", "-480-0.webp", src))
        if not fotos:
            continue

        nombre = (p.get("name") or {}).get("es") or "Camiseta"
        imagenes[p["id"]] = fotos
        catalogo.append(
            {
                "id": p["id"],
                "nombre": nombre_limpio(nombre),
                "cats": categorias_de(p),
                "version": version_de(nombre),
                "fotos": len(fotos),
                "talles": talles,
            }
        )
    return catalogo, imagenes


def _actualizar_si_hace_falta() -> None:
    with _candado:
        if _cache["catalogo"] is not None and time.time() - _cache["t"] < CACHE_SEGUNDOS:
            return
        try:
            catalogo, imagenes = armar_catalogo(_traer_productos())
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo leer Tiendanube: %s", e)
            if _cache["catalogo"] is None:
                raise
            # Hay una versión anterior: mejor mostrarla que dejar la web caída.
            _cache["t"] = time.time() - CACHE_SEGUNDOS + 30
            return
        _cache.update(t=time.time(), catalogo=catalogo, imagenes=imagenes)


# -- La app -------------------------------------------------------------------

app = FastAPI(title="Tribuneros Mayorista", docs_url=None, redoc_url=None)


@app.get("/salud")
def salud() -> dict:
    return {"estado": "ok"}


@app.get("/api/catalogo")
def api_catalogo(response: Response) -> dict:
    try:
        _actualizar_si_hace_falta()
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "No pudimos cargar el catálogo. Probá de nuevo en un minuto.") from None
    response.headers["Cache-Control"] = "no-store"
    return {
        "productos": _cache["catalogo"],
        "tramos": TRAMOS,
        "minimo": MINIMO,
        "limitarStock": LIMITAR_STOCK,
        "whatsapp": WHATSAPP,
    }


@app.get("/img/{producto}/{n}")
def imagen(producto: int, n: int) -> Response:
    try:
        _actualizar_si_hace_falta()
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "Sin catálogo") from None
    urls = _cache["imagenes"].get(producto) or []
    if n < 1 or n > len(urls):
        raise HTTPException(404, "No existe esa foto")
    url = urls[n - 1]
    if url not in _fotos:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=CABECERAS_CDN), timeout=30) as r:
                _fotos[url] = r.read()
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo bajar la foto %s: %s", url, e)
            raise HTTPException(502, "No se pudo cargar la foto") from None
    return Response(_fotos[url], media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


# La página y el logo, al final: lo de arriba tiene prioridad sobre los archivos.
app.mount("/", StaticFiles(directory=CARPETA / "static", html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=PUERTO, log_level="info")
