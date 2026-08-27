"""Levanta la plataforma de pruebas en el navegador.

    python servidor.py

Después abrí:  http://localhost:8000
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from agente.consola import preparar  # noqa: E402

preparar()  # antes de imprimir nada, para que las tildes no rompan Windows

import uvicorn  # noqa: E402

from agente.config import proveedores_disponibles  # noqa: E402

PUERTO = 8000


def main() -> None:
    listos = [n for n, ok in proveedores_disponibles().items() if ok]

    lineas = [
        "",
        "  AgentKit - plataforma de pruebas",
        "  --------------------------------",
        "",
        f"  Abri:  http://localhost:{PUERTO}",
        "",
    ]

    if listos:
        lineas.append(f"  Proveedores listos: {', '.join(listos)}")
    else:
        lineas += [
            "  [!] Todavia no hay ninguna clave cargada.",
            "      Abri el archivo .env y completa la de un proveedor",
            "      (con uno alcanza). Despues recarga la pagina.",
        ]

    lineas += ["", "  Para cortar: Ctrl+C", ""]

    # flush=True porque si no, Python se guarda el texto y no ves nada.
    print("\n".join(lineas), flush=True)

    uvicorn.run("agente.web.app:app", host="127.0.0.1", port=PUERTO, log_level="warning")


if __name__ == "__main__":
    main()
