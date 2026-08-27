"""Carga del prompt del sistema desde un archivo de texto.

El prompt vive en prompts/sistema.md, no adentro del código. Dos motivos:
lo podés editar sin tocar Python, y se relee en cada mensaje — o sea que
cambiás la personalidad del agente con el agente andando.
"""

from __future__ import annotations

from pathlib import Path

PROMPT_DE_EMERGENCIA = (
    "Sos un asistente útil. Respondé en español, de forma clara y directa."
)


def leer_prompt(ruta: Path) -> str:
    """Lee el prompt del sistema. Si el archivo no está, usa uno mínimo."""
    try:
        texto = ruta.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return PROMPT_DE_EMERGENCIA

    return texto or PROMPT_DE_EMERGENCIA


def guardar_prompt(ruta: Path, texto: str) -> None:
    """Guarda el prompt del sistema (lo usa el editor de la plataforma web)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto.strip() + "\n", encoding="utf-8")
