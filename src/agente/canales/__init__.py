"""Los canales por donde el agente atiende.

    base.py       la forma que tiene un canal
    telegram.py   próximo video
    whatsapp.py   el que viene después

El agente no cambia entre uno y otro: cada canal es un archivo nuevo que
lo usa.
"""

from __future__ import annotations

from .base import Canal, MensajeEntrante

__all__ = ["Canal", "MensajeEntrante"]
