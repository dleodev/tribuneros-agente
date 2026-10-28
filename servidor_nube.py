"""La plataforma de pruebas, pero para correr en un contenedor.

    python servidor_nube.py

Es lo mismo que `servidor.py` (ver ese archivo), con las diferencias que
hacen falta para vivir en un contenedor en vez de en tu compu:

- Escucha en 0.0.0.0, no en 127.0.0.1: adentro de un contenedor, localhost es
  un lugar al que nadie de afuera puede entrar.
- El puerto sale de la variable PUERTO (como el webhook), no está fijo.
- Pide usuario y clave (PRUEBAS_USUARIO / PRUEBAS_CLAVE, ver acceso.py) y
  **no arranca si faltan**: en un dominio público, sin clave, cualquiera
  podría chatear gastando tus tokens y editar el prompt. Mejor que se caiga
  al desplegar —donde se nota— a que quede abierto sin que nadie se entere.
- Tiene /salud, sin clave, para el health check del contenedor.

Acá vive también el botón "Guardar definitivo" del editor de prompt (ver
`src/agente/web/publicar_prompt.py`), que es lo que lleva un cambio hecho acá
hasta el webhook real de WhatsApp. Sin las variables GITHUB_TOKEN,
GITHUB_REPO, COOLIFY_URL, COOLIFY_TOKEN y COOLIFY_APP_UUID cargadas, ese botón
queda oculto solo — así que corriendo local (`python servidor.py`) nunca
aparece.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from agente.consola import preparar  # noqa: E402

preparar()  # antes de imprimir nada, para que las tildes no rompan Windows

import logging  # noqa: E402

import uvicorn  # noqa: E402

PUERTO = int(os.getenv("PUERTO", "8000"))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    usuario = os.getenv("PRUEBAS_USUARIO", "")
    clave = os.getenv("PRUEBAS_CLAVE", "")
    if not usuario or not clave:
        print(
            "Faltan PRUEBAS_USUARIO y PRUEBAS_CLAVE. La plataforma de pruebas "
            "en la nube no se levanta sin usuario y clave: queda en un dominio "
            "público y cualquiera podría usar tu agente y editar el prompt."
        )
        return 1

    from agente.web.acceso import AccesoBasico
    from agente.web.app import app

    @app.get("/salud")
    def salud() -> dict:
        return {"estado": "ok"}

    app.add_middleware(AccesoBasico, usuario=usuario, clave=clave)
    uvicorn.run(app, host="0.0.0.0", port=PUERTO, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
