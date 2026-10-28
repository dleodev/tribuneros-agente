"""Usuario y clave para la plataforma de pruebas desplegada en la nube.

La plataforma local (`servidor.py`) no pide nada porque solo escucha en
127.0.0.1. En la nube escucha en un dominio público: sin esto, cualquiera que
encuentre la URL puede chatear con el agente (gastando tus tokens) y editar el
prompt.

Va en la app y no en etiquetas del proxy a propósito: las etiquetas dependen de
cómo arme las rutas cada proveedor (hubo que escapar un hash con `$$` a mano
y aun así falló una vez), y se pierden al mudar de servidor. Acá la clave viaja
con el código.

Es un middleware ASGI puro y no `BaseHTTPMiddleware`: ese rompe las respuestas
en streaming, y el chat de la plataforma es un stream.
"""

from __future__ import annotations

import base64
import secrets

# El health check del contenedor no manda credenciales.
RUTAS_LIBRES = {"/salud"}


class AccesoBasico:
    def __init__(self, app, usuario: str, clave: str) -> None:
        self.app = app
        self._esperado = f"{usuario}:{clave}".encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        tipo = scope["type"]

        if tipo == "lifespan" or scope.get("path") in RUTAS_LIBRES:
            await self.app(scope, receive, send)
            return

        if tipo == "http":
            if self._autorizado(scope):
                await self.app(scope, receive, send)
            else:
                await self._pedir_clave(send)
            return

        if tipo == "websocket":
            # La plataforma no usa websockets: no hay nada legítimo que dejar pasar.
            await send({"type": "websocket.close", "code": 1008})

    def _autorizado(self, scope) -> bool:
        for nombre, valor in scope["headers"]:
            if nombre != b"authorization":
                continue
            esquema, _, credencial = valor.partition(b" ")
            if esquema.lower() != b"basic":
                return False
            try:
                dado = base64.b64decode(credencial, validate=True)
            except ValueError:
                return False
            # compare_digest: que la comparación no tarde distinto según
            # cuántos caracteres coinciden.
            return secrets.compare_digest(dado, self._esperado)
        return False

    @staticmethod
    async def _pedir_clave(send) -> None:
        cuerpo = "Hace falta usuario y clave.".encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="Tribuneros"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(cuerpo)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": cuerpo})
