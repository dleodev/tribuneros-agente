"""Que la terminal no se rompa con las tildes.

En Windows, la consola arranca con una codificación vieja (cp1252) que no
sabe escribir ni una "í". Si imprimís algo con tilde, el programa se cae
con UnicodeEncodeError antes de hacer nada.

Esta función se llama al principio de chat.py y de servidor.py.
"""

from __future__ import annotations

import sys


def preparar() -> None:
    """Pone la terminal en UTF-8. Si no se puede, al menos que no se caiga."""
    for flujo in (sys.stdout, sys.stderr):
        reconfigurar = getattr(flujo, "reconfigure", None)
        if reconfigurar is None:
            continue
        try:
            # errors="replace" es la red de seguridad: si algún caracter no
            # entra, se dibuja un signo de pregunta en vez de tirar el programa.
            reconfigurar(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
