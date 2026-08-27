"""El agente atendiendo en Telegram.

    python bot_telegram.py

Corre en tu computadora: no hace falta hosting, ni dominio, ni abrir puertos.
El programa le pregunta a Telegram si hay mensajes nuevos (polling), así que
mientras esta ventana esté abierta, el bot contesta.

Antes de arrancar, en el .env:

    TELEGRAM_TOKEN=  ← el que te da @BotFather

Para cortar: Ctrl+C.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from agente.consola import preparar  # noqa: E402

preparar()  # antes de imprimir nada, para que las tildes no rompan Windows

from agente import Agente, Config, ErrorDeConfiguracion  # noqa: E402
from agente.canales.telegram import Telegram  # noqa: E402

AMBAR = "\033[38;5;214m"
GRIS = "\033[90m"
ROJO = "\033[91m"
FIN = "\033[0m"


def main() -> int:
    try:
        config = Config.desde_entorno()
        canal = Telegram(config.telegram_token)
    except (ErrorDeConfiguracion, ValueError) as e:
        print(f"{ROJO}{e}{FIN}")
        return 1

    agente = Agente(config)

    try:
        quien = canal.yo_soy()
    except Exception as e:
        print(f"{ROJO}No se pudo hablar con Telegram: {type(e).__name__}: {e}{FIN}")
        print(f"{GRIS}Revisá el TELEGRAM_TOKEN del .env.{FIN}")
        return 1

    memoria = "Postgres" if config.modo == "produccion" else "SQLite"

    print(f"\n{AMBAR}Bot escuchando{FIN} - @{quien.get('username', '?')}")
    print(f"{GRIS}   {config.proveedor} - {config.modelo} - memoria {memoria}{FIN}")
    print(f"{GRIS}   Escribile por Telegram. Para cortar: Ctrl+C.{FIN}\n")

    try:
        atender(agente, canal)
    except KeyboardInterrupt:
        print(f"\n{GRIS}Listo, cortamos.{FIN}")

    return 0


def atender(agente: Agente, canal: Telegram) -> None:
    """El bucle: llega un mensaje, contesta el agente, sale por el canal.

    Estas líneas son las mismas para cualquier canal. Lo único que cambia de
    Telegram a WhatsApp es de dónde salen los mensajes y por dónde se mandan
    — el agente ni se entera.
    """
    for entrante in canal.escuchar():
        if not canal.deberia_responder(entrante):
            continue

        print(f"{GRIS}[{entrante.conversacion}]{FIN} {entrante.texto}")

        # El "escribiendo..." mientras el modelo piensa.
        canal.escribiendo(entrante.conversacion)

        try:
            # LA línea. `conversacion` es el chat_id, y es lo que hace que
            # cada persona tenga su propia memoria sin mezclarse con la de al
            # lado. En la web era siempre "web" porque había un solo usuario.
            mensajes = agente.responder_partido(
                entrante.texto, conversacion=entrante.conversacion
            )
        except Exception as e:
            # El error del proveedor no se esconde: se lo decimos a la persona
            # y lo dejamos en la terminal. Pero no volteamos el bot, porque
            # atiende a varias personas y un error con una no puede dejar sin
            # respuesta a las demás.
            aviso = f"{type(e).__name__}: {e}"
            print(f"{ROJO}   {aviso}{FIN}")
            canal.enviar(entrante.conversacion, [f"Se me rompió algo: {aviso}"])
            continue

        canal.enviar(entrante.conversacion, mensajes)
        print(f"{AMBAR}   -> {len(mensajes)} mensaje(s){FIN}")


if __name__ == "__main__":
    raise SystemExit(main())
