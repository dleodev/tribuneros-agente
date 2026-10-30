"""El panel de administración del mayorista: stock como una planilla.

Todo cuelga de /admin/api y exige la sesión de `acceso`. Lo que se edita es el
stock de Tiendanube (la única fuente de verdad), así que la tienda minorista y
la mayorista siempre muestran lo mismo.

Cómo se protege un guardado:

  · Cada cambio viaja con el valor que la persona VIO ("antes"). Si en el medio
    entró una venta y el stock ya no es ese, no se pisa: se avisa (409) y no se
    aplica nada. Todo o nada, para que un cambio de a varios no quede a medias.
  · Cada guardado queda en el historial, y se puede deshacer entero.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import acceso
import historial as hist
import tn

log = logging.getLogger("mayorista.admin")

MAXIMO_DE_CAMBIOS = 500
MAXIMO_DE_STOCK = 9999
MAXIMO_DE_FOTOS = 4
PESO_MAXIMO_DE_FOTO = 5 * 1024 * 1024
TIPOS_DE_FOTO = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")  # jpeg, png, webp

ORDEN_DE_TALLES = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]

# La primera foto de cada modelo, de la última vez que se listó la planilla.
_foto_de_producto: dict[int, str] = {}


# -- Lo que llega -------------------------------------------------------------


class Login(BaseModel):
    usuario: str = Field(max_length=100)
    clave: str = Field(max_length=200)


class Cambio(BaseModel):
    tipo: Literal["stock", "talle_nuevo"]
    producto: int
    variante: int | None = None
    talle: str | None = Field(default=None, max_length=40)
    antes: int | None = None
    despues: int = Field(ge=0, le=MAXIMO_DE_STOCK)


class Aplicar(BaseModel):
    cambios: list[Cambio] = Field(max_length=MAXIMO_DE_CAMBIOS)


class Foto(BaseModel):
    nombre: str = Field(max_length=120)
    datos: str  # base64, con o sin el prefijo "data:image/...;base64,"


class NuevoModelo(BaseModel):
    nombre: str = Field(min_length=2, max_length=200)
    precio: float = Field(gt=0, lt=100_000_000)
    talles: dict[str, int]
    categorias: list[int] = []
    publicado: bool = False
    fotos: list[Foto] = Field(default=[], max_length=MAXIMO_DE_FOTOS)


# -- Ayudantes ----------------------------------------------------------------


def talle_de(variante: dict) -> str:
    valores = variante.get("values") or []
    return ", ".join(v.get("es", "") for v in valores if v.get("es")) or "Único"


def _clave_de_talle(talle: str) -> tuple[int, str]:
    base = talle.split(" - ")[0].strip().upper()
    return (ORDEN_DE_TALLES.index(base) if base in ORDEN_DE_TALLES else 99, talle)


def _ubicacion(variante: dict) -> str | None:
    niveles = variante.get("inventory_levels") or []
    return niveles[0].get("location_id") if niveles else None


def vista_de_producto(p: dict) -> dict:
    variantes = [
        {
            "id": v["id"],
            "talle": talle_de(v),
            # None = Tiendanube no lleva la cuenta de esta variante: no se edita.
            "stock": v.get("stock") if v.get("stock_management", True) else None,
        }
        for v in p.get("variants") or []
    ]
    variantes.sort(key=lambda v: _clave_de_talle(v["talle"]))
    precio = next((v.get("price") for v in p.get("variants") or [] if v.get("price")), None)
    return {
        "id": p["id"],
        "nombre": (p.get("name") or {}).get("es") or "Sin nombre",
        "publicado": bool(p.get("published")),
        "categorias": [(c.get("name") or {}).get("es", "") for c in p.get("categories") or []],
        "precio": precio,
        "foto": bool(p.get("images")),
        "variantes": variantes,
    }


def _normalizar(talle: str) -> str:
    return " ".join(talle.upper().split())


def decodificar_foto(foto: Foto) -> dict:
    """Valida una foto subida y la deja como la pide Tiendanube."""
    datos = foto.datos.split(",", 1)[1] if foto.datos.startswith("data:") else foto.datos
    try:
        crudo = base64.b64decode(datos, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, f"La foto «{foto.nombre}» no se pudo leer.") from None
    if len(crudo) > PESO_MAXIMO_DE_FOTO:
        raise HTTPException(422, f"La foto «{foto.nombre}» pesa más de 5 MB.")
    if not crudo.startswith(TIPOS_DE_FOTO):
        raise HTTPException(422, f"La foto «{foto.nombre}» tiene que ser JPG, PNG o WebP.")
    nombre = "".join(c for c in foto.nombre if c.isalnum() or c in "._-") or "foto"
    return {"filename": nombre, "attachment": base64.b64encode(crudo).decode()}


# -- El router ----------------------------------------------------------------


def crear_router(historial, invalidar_cache: Callable[[], None]) -> APIRouter:
    router = APIRouter(prefix="/admin/api")

    def exigir_panel() -> None:
        if not acceso.configurado() or historial is None or not tn.configurado():
            raise HTTPException(503, "El panel no está configurado en el servidor.")

    def usuario_actual(request: Request, _=Depends(exigir_panel)) -> str:
        usuario = acceso.leer_sesion(request.cookies.get(acceso.COOKIE))
        if not usuario:
            raise HTTPException(401, "Iniciá sesión.")
        # Los cambios exigen un header propio: un sitio ajeno no puede mandarlo
        # desde el navegador sin permiso, y así nadie guarda cosas por vos.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-mayorista") != "1":
            raise HTTPException(403, "Pedido no permitido.")
        return usuario

    # -- Sesión --

    @router.get("/estado")
    def estado(request: Request) -> dict:
        """Para que la página sepa si mostrar el login, sin dar error."""
        if not acceso.configurado() or historial is None or not tn.configurado():
            return {"configurado": False, "usuario": None}
        return {"configurado": True, "usuario": acceso.leer_sesion(request.cookies.get(acceso.COOKIE))}

    @router.post("/login")
    def login(datos: Login, request: Request, response: Response, _=Depends(exigir_panel)) -> dict:
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
        if acceso.bloqueado(ip):
            raise HTTPException(429, "Demasiados intentos. Probá de nuevo en unos minutos.")
        if not acceso.credenciales_validas(datos.usuario.strip(), datos.clave):
            acceso.anotar_fallo(ip)
            raise HTTPException(401, "Usuario o clave incorrectos.")
        acceso.limpiar_fallos(ip)
        response.set_cookie(
            acceso.COOKIE,
            acceso.crear_sesion(acceso.USUARIO),
            max_age=acceso.DURACION_SEGUNDOS,
            httponly=True,
            samesite="strict",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
            path="/",
        )
        return {"usuario": acceso.USUARIO}

    @router.post("/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie(acceso.COOKIE, path="/")
        return {"ok": True}

    # -- Leer --

    @router.get("/stock")
    def stock(usuario: str = Depends(usuario_actual)) -> dict:
        try:
            productos = tn.todos_los_productos()
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        for p in productos:
            imagenes = sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)
            if imagenes:
                _foto_de_producto[p["id"]] = imagenes[0]["src"]
        return {"productos": [vista_de_producto(p) for p in productos], "stock_maximo": MAXIMO_DE_STOCK}

    @router.get("/foto/{producto}")
    def foto(producto: int, usuario: str = Depends(usuario_actual)) -> Response:
        url = _foto_de_producto.get(producto)
        if not url:
            raise HTTPException(404, "Sin foto")
        try:
            contenido = tn.bajar_foto(tn.miniatura(url, 240))
        except OSError:
            raise HTTPException(502, "No se pudo cargar la foto") from None
        return Response(contenido, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/categorias")
    def categorias(usuario: str = Depends(usuario_actual)) -> list[dict]:
        try:
            todas = tn.categorias()
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        nombres = {c["id"]: (c.get("name") or {}).get("es", "") for c in todas}
        return [
            {"id": c["id"], "nombre": (f"{nombres.get(c['parent'], '')} › " if c.get("parent") else "") + nombres[c["id"]]}
            for c in todas
        ]

    # -- Guardar --

    def _aplicar(usuario: str, cambios: list[Cambio], etiqueta: str = "") -> dict:
        """El corazón: valida contra el stock de AHORA y aplica todo o nada."""
        if not cambios:
            raise HTTPException(422, "No hay cambios para guardar.")
        try:
            actuales = tn.productos_por_id([c.producto for c in cambios])
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None

        conflictos: list[dict] = []
        stocks: list[tuple[int, int, int, str | None]] = []
        altas: list[tuple[int, str, int, str]] = []
        entradas: list[dict] = []

        for c in cambios:
            p = actuales.get(c.producto)
            if not p:
                conflictos.append({"producto": c.producto, "motivo": "El modelo ya no existe."})
                continue
            nombre = (p.get("name") or {}).get("es", "")
            variantes = {v["id"]: v for v in p.get("variants") or []}

            if c.tipo == "stock":
                v = variantes.get(c.variante or 0)
                if not v:
                    conflictos.append({"producto": c.producto, "modelo": nombre, "motivo": "Ese talle ya no existe."})
                    continue
                if v.get("stock") is None:
                    conflictos.append({"producto": c.producto, "modelo": nombre, "talle": talle_de(v), "motivo": "Tiendanube no lleva la cuenta de este talle."})
                    continue
                if v["stock"] != c.antes:
                    conflictos.append(
                        {"producto": c.producto, "modelo": nombre, "talle": talle_de(v), "esperado": c.antes, "actual": v["stock"],
                         "motivo": "El stock cambió mientras editabas."}
                    )
                    continue
                if c.despues == v["stock"]:
                    continue
                stocks.append((c.producto, v["id"], c.despues, _ubicacion(v)))
                entradas.append({"accion": "stock", "producto_id": c.producto, "variante_id": v["id"], "producto": nombre, "talle": talle_de(v), "antes": v["stock"], "despues": c.despues})
            else:
                talle = (c.talle or "").strip()
                if not talle:
                    conflictos.append({"producto": c.producto, "modelo": nombre, "motivo": "Falta el talle."})
                    continue
                if any(_normalizar(talle_de(v)) == _normalizar(talle) for v in variantes.values()):
                    conflictos.append({"producto": c.producto, "modelo": nombre, "talle": talle, "motivo": "Ese talle ya existe."})
                    continue
                precio = next((v.get("price") for v in variantes.values() if v.get("price")), None)
                if not precio:
                    conflictos.append({"producto": c.producto, "modelo": nombre, "talle": talle, "motivo": "No hay precio de referencia para el talle nuevo."})
                    continue
                if c.despues == 0:
                    continue
                altas.append((c.producto, talle, c.despues, precio))
                entradas.append({"accion": "talle_nuevo", "producto_id": c.producto, "producto": nombre, "talle": talle, "antes": None, "despues": c.despues})

        if conflictos:
            raise HTTPException(409, detail={"mensaje": "No se guardó nada: algo cambió desde que abriste la planilla.", "conflictos": conflictos})
        if not stocks and not altas:
            return {"lote": None, "aplicados": 0, "creados": 0}

        lote = hist.lote_nuevo()
        hechos = 0
        try:
            # De a 50, como pide Tiendanube, anotando cada bloque apenas se aplica.
            for i in range(0, len(stocks), tn.MAXIMO_POR_LOTE):
                trozo = stocks[i : i + tn.MAXIMO_POR_LOTE]
                tn.actualizar_stock(trozo)
                ids = {v for _, v, _, _ in trozo}
                historial.anotar(lote, usuario, [e for e in entradas if e["accion"] == "stock" and e["variante_id"] in ids])
                hechos += len(trozo)
            for producto, talle, cantidad, precio in altas:
                tn.crear_variante(producto, talle, cantidad, precio)
                historial.anotar(lote, usuario, [e for e in entradas if e["accion"] == "talle_nuevo" and e["producto_id"] == producto and e["talle"] == talle])
                hechos += 1
        except tn.ErrorDeTiendanube as e:
            invalidar_cache()
            raise HTTPException(502, f"Se guardaron {hechos} cambios y después falló Tiendanube: {e}. Revisá el historial y recargá la planilla.") from None

        invalidar_cache()
        return {"lote": lote, "aplicados": len(stocks), "creados": len(altas)}

    @router.post("/stock/aplicar")
    def aplicar(datos: Aplicar, usuario: str = Depends(usuario_actual)) -> dict:
        return _aplicar(usuario, datos.cambios)

    # -- Historial --

    @router.get("/historial")
    def ver_historial(usuario: str = Depends(usuario_actual)) -> list[dict]:
        return historial.lotes(30)

    @router.post("/historial/{lote}/deshacer")
    def deshacer(lote: str, usuario: str = Depends(usuario_actual)) -> dict:
        entradas = [e for e in historial.entradas_del_lote(lote) if e["accion"] in hist.REVERSIBLES and not e["deshecho"]]
        if not entradas:
            raise HTTPException(404, "No hay nada para deshacer en ese guardado.")
        cambios = [
            Cambio(tipo="stock", producto=e["producto_id"], variante=e["variante_id"] or _variante_de(e),
                   antes=e["despues"], despues=e["antes"] or 0)
            for e in entradas
        ]
        # Las altas de talle se deshacen dejando el talle en 0: el talle queda, vacío.
        resultado = _aplicar(usuario, cambios)
        historial.marcar_deshecho(lote)
        return resultado

    def _variante_de(entrada: dict) -> int:
        # Un talle creado en un guardado no guardó el id de su variante: se busca por nombre.
        p = tn.productos_por_id([entrada["producto_id"]]).get(entrada["producto_id"]) or {}
        for v in p.get("variants") or []:
            if _normalizar(talle_de(v)) == _normalizar(entrada.get("talle") or ""):
                return v["id"]
        raise HTTPException(409, detail={"mensaje": "No se encontró el talle creado.", "conflictos": []})

    # -- Modelos nuevos --

    @router.post("/productos")
    def nuevo_modelo(datos: NuevoModelo, usuario: str = Depends(usuario_actual)) -> dict:
        talles = {}
        for talle, cantidad in datos.talles.items():
            talle = " ".join(talle.split())
            if not talle or len(talle) > 40:
                raise HTTPException(422, "Hay un talle con nombre inválido.")
            if not 0 <= cantidad <= MAXIMO_DE_STOCK:
                raise HTTPException(422, f"El stock de {talle} tiene que estar entre 0 y {MAXIMO_DE_STOCK}.")
            talles[talle] = cantidad
        if not talles:
            raise HTTPException(422, "Cargá al menos un talle.")
        fotos = [decodificar_foto(f) for f in datos.fotos]

        try:
            creado = tn.crear_producto(
                nombre=" ".join(datos.nombre.split()),
                precio=f"{datos.precio:.2f}",
                talles=talles,
                categorias_ids=datos.categorias,
                fotos=fotos,
                publicado=datos.publicado,
            )
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None

        historial.anotar(
            hist.lote_nuevo(),
            usuario,
            [{"accion": "producto_nuevo", "producto_id": creado["id"], "producto": datos.nombre, "talle": ", ".join(f"{t} x{n}" for t, n in talles.items()), "antes": None, "despues": sum(talles.values())}],
        )
        invalidar_cache()
        return {"id": creado["id"], "nombre": datos.nombre, "publicado": datos.publicado}

    return router
