"""El login del panel de administración.

Un usuario y una clave que salen de variables de entorno (ADMIN_USUARIO y
ADMIN_CLAVE). Al entrar, el navegador recibe una cookie firmada con
SESION_SECRETO que vence sola. No hay base de usuarios: no hace falta para uno.

Sin esas tres variables el panel queda apagado (ver `configurado`): es mejor
que no exista a que exista sin clave.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

USUARIO = os.getenv("ADMIN_USUARIO", "").strip()
CLAVE = os.getenv("ADMIN_CLAVE", "")
SECRETO = os.getenv("SESION_SECRETO", "")

COOKIE = "mayorista_sesion"
DURACION_SEGUNDOS = 12 * 60 * 60

# Anti fuerza bruta: tantos intentos fallidos por IP en la ventana, y se bloquea.
MAXIMO_DE_FALLOS = 5
VENTANA_SEGUNDOS = 15 * 60
_fallos: dict[str, list[float]] = {}


def configurado() -> bool:
    # Un secreto corto se adivina: se exige largo.
    return bool(USUARIO and CLAVE and len(SECRETO) >= 24)


def _firmar(contenido: bytes) -> str:
    return hmac.new(SECRETO.encode(), contenido, hashlib.sha256).hexdigest()


def crear_sesion(usuario: str, ahora: float | None = None) -> str:
    cuerpo = base64.urlsafe_b64encode(
        json.dumps({"u": usuario, "e": int((ahora or time.time()) + DURACION_SEGUNDOS)}).encode()
    )
    return f"{cuerpo.decode()}.{_firmar(cuerpo)}"


def leer_sesion(valor: str | None, ahora: float | None = None) -> str | None:
    """El usuario de la cookie, o None si falta, está adulterada o venció."""
    if not valor or "." not in valor or not configurado():
        return None
    cuerpo, firma = valor.rsplit(".", 1)
    if not hmac.compare_digest(firma, _firmar(cuerpo.encode())):
        return None
    try:
        datos = json.loads(base64.urlsafe_b64decode(cuerpo.encode()))
    except ValueError:
        return None
    if datos.get("e", 0) < (ahora or time.time()) or datos.get("u") != USUARIO:
        return None
    return datos["u"]


def credenciales_validas(usuario: str, clave: str) -> bool:
    """Compara las dos siempre, para que el tiempo no diga cuál estaba mal."""
    a = hmac.compare_digest(usuario.encode(), USUARIO.encode())
    b = hmac.compare_digest(clave.encode(), CLAVE.encode())
    return a and b


# -- Bloqueo por intentos ------------------------------------------------------


def bloqueado(ip: str, ahora: float | None = None) -> bool:
    ahora = ahora or time.time()
    recientes = [t for t in _fallos.get(ip, []) if ahora - t < VENTANA_SEGUNDOS]
    _fallos[ip] = recientes
    return len(recientes) >= MAXIMO_DE_FALLOS


def anotar_fallo(ip: str, ahora: float | None = None) -> None:
    _fallos.setdefault(ip, []).append(ahora or time.time())


def limpiar_fallos(ip: str) -> None:
    _fallos.pop(ip, None)


def clave_nueva() -> str:
    """Para generar una ADMIN_CLAVE o un SESION_SECRETO."""
    return secrets.token_urlsafe(32)
