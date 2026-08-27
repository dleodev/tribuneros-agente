"""Pruebas del canal de Telegram. No salen a internet ni gastan tokens.

La API de Telegram se reemplaza por una de mentira que anota lo que se le
pidió. Lo que se prueba es lo nuestro: qué mensajes se atienden, cómo se
traducen y qué sale para el otro lado.

    pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agente.canales.telegram import (  # noqa: E402
    LARGO_MAXIMO,
    Telegram,
    _cortar,
    _traducir,
)


def novedad(texto="hola", chat=555, update=1, es_bot=False, tipo="text") -> dict:
    """Un mensaje como lo manda Telegram."""
    mensaje = {"chat": {"id": chat}, "from": {"id": 9, "is_bot": es_bot}}
    if tipo == "text":
        mensaje["text"] = texto
    else:
        mensaje[tipo] = {"file_id": "xxx"}  # una foto, un audio, lo que sea

    return {"update_id": update, "message": mensaje}


class TelegramFalso(Telegram):
    """El canal, pero con la API de mentira. Anota todo lo que se mandó."""

    def __init__(self, tandas=None):
        super().__init__(token="123:falso")
        self.enviados: list[dict] = []
        self._tandas = list(tandas or [])

    def _api(self, metodo, datos=None, espera=30):
        self.enviados.append({"metodo": metodo, "datos": datos or {}})

        if metodo == "getUpdates":
            return {"result": self._tandas.pop(0) if self._tandas else []}
        return {"ok": True, "result": {}}


# -- Traducir lo que llega ----------------------------------------------------


def test_traduce_un_mensaje_normal():
    entrante = _traducir(novedad("¿que clima hace?", chat=777, update=42))

    assert entrante is not None
    assert entrante.texto == "¿que clima hace?"
    assert entrante.conversacion == "777", "el chat_id es el thread_id"
    assert entrante.identificador == "42"


def test_el_chat_id_va_como_texto():
    """Es el thread_id de LangGraph, y ahí un 555 y un "555" no son lo mismo."""
    assert isinstance(_traducir(novedad(chat=555)).conversacion, str)


@pytest.mark.parametrize("tipo", ["photo", "voice", "sticker", "document"])
def test_lo_que_no_es_texto_se_deja_pasar(tipo):
    """El agente todavía no sabe leer fotos ni escuchar audios."""
    assert _traducir(novedad(tipo=tipo)) is None


def test_no_le_contesta_a_otro_bot():
    """Dos bots hablándose es un ida y vuelta que no termina más."""
    assert _traducir(novedad(es_bot=True)) is None


# -- Qué se contesta y qué no -------------------------------------------------


def test_no_contesta_dos_veces_el_mismo_mensaje():
    """Telegram reenvía ante la duda. Sin esto, el agente contesta repetido."""
    canal = TelegramFalso()
    entrante = _traducir(novedad(update=7))

    assert canal.deberia_responder(entrante) is True
    assert canal.deberia_responder(entrante) is False, "la segunda vez ya no"


def test_ignora_los_mensajes_vacios():
    canal = TelegramFalso()
    assert canal.deberia_responder(_traducir(novedad("   "))) is False


# -- El bucle de escucha ------------------------------------------------------


def test_escuchar_devuelve_los_mensajes_traducidos():
    canal = TelegramFalso(tandas=[[novedad("hola", update=1)]])

    primero = next(canal.escuchar())

    assert primero.texto == "hola"


def test_el_offset_avanza_aunque_el_mensaje_no_sirva():
    """Si no avanzara, un mensaje que no sabemos atender vuelve para siempre.

    Telegram reenvía todo lo que no le confirmaste. Una foto en el medio
    dejaría al bot trabado ahí, sin contestarle nunca más a nadie.
    """
    canal = TelegramFalso(
        tandas=[
            [novedad(tipo="photo", update=10)],  # esta no se puede atender
            [novedad("hola", update=11)],
        ]
    )

    escucha = canal.escuchar()
    primero = next(escucha)

    assert primero.texto == "hola", "tendría que haber seguido de largo"
    assert canal._proxima == 12, "el offset tiene que haber pasado la foto"


# -- Lo que sale --------------------------------------------------------------


def test_envia_cada_mensaje_por_separado():
    canal = TelegramFalso()

    canal.enviar("555", ["primero", "segundo"])

    envios = [e for e in canal.enviados if e["metodo"] == "sendMessage"]
    assert [e["datos"]["text"] for e in envios] == ["primero", "segundo"]
    assert all(e["datos"]["chat_id"] == "555" for e in envios)


def test_un_mensaje_gigante_se_corta_antes_de_mandarlo():
    """Telegram rechaza el mensaje entero si se pasa del largo."""
    canal = TelegramFalso()

    canal.enviar("555", ["palabra " * 2000])

    envios = [e for e in canal.enviados if e["metodo"] == "sendMessage"]
    assert len(envios) > 1, "tendría que haberlo partido"
    assert all(len(e["datos"]["text"]) <= LARGO_MAXIMO for e in envios)


def test_el_escribiendo_no_voltea_la_respuesta(monkeypatch):
    """Es cosmético: si falla, la respuesta tiene que salir igual."""
    canal = TelegramFalso()

    def explota(*a, **k):
        raise ConnectionError("se cayó")

    monkeypatch.setattr(canal, "_api", explota)
    canal.escribiendo("555")  # no tiene que levantar nada


# -- Cortar textos largos -----------------------------------------------------


def test_cortar_respeta_el_largo():
    pedazos = _cortar("a" * 100, largo=30)
    assert all(len(p) <= 30 for p in pedazos)
    assert "".join(pedazos) == "a" * 100


def test_cortar_no_parte_palabras_al_medio():
    pedazos = _cortar("hola " * 40, largo=50)
    assert not any(p.endswith("hol") or p.startswith("la ") for p in pedazos)


def test_cortar_deja_en_paz_lo_que_ya_entra():
    assert _cortar("corto", largo=100) == ["corto"]


# -- Configuración ------------------------------------------------------------


def test_sin_token_avisa_que_falta():
    with pytest.raises(ValueError, match="TELEGRAM_TOKEN"):
        Telegram(token="")


def test_el_token_sale_del_env(monkeypatch):
    """Sin esto el token está en el .env pero no lo lee nadie."""
    from agente.config import Config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "clave-de-prueba")
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")

    assert Config.desde_entorno("claude").telegram_token == "123:abc"


# -- El bucle entero, de punta a punta ----------------------------------------
#
# Acá se pegan todas las piezas: llega un mensaje de Telegram, contesta el
# agente, sale la respuesta. Con un canal de mentira y un modelo de mentira,
# así que no toca la red ni gasta un token.

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot_telegram import atender  # noqa: E402

from test_agente import agente_falso  # noqa: E402  (el agente con modelo falso)


class TelegramQueTermina(TelegramFalso):
    """Como el falso, pero corta cuando se le acaban los mensajes.

    El bucle de verdad es infinito a propósito (un bot escucha para siempre),
    así que para poder probarlo lo cortamos igual que Ctrl+C.
    """

    def _traer_novedades(self):
        if not self._tandas:
            raise KeyboardInterrupt
        return self._tandas.pop(0)


def test_el_mensaje_da_la_vuelta_completa():
    canal = TelegramQueTermina(tandas=[[novedad("hola", chat=555, update=1)]])
    agente = agente_falso(["¡Buenas! ¿En qué te ayudo?"])

    with pytest.raises(KeyboardInterrupt):  # así corta el bot
        atender(agente, canal)

    enviados = [e for e in canal.enviados if e["metodo"] == "sendMessage"]
    assert len(enviados) == 1
    assert enviados[0]["datos"]["text"] == "¡Buenas! ¿En qué te ayudo?"
    assert enviados[0]["datos"]["chat_id"] == "555"


def test_cada_chat_tiene_su_propia_memoria():
    """Lo único que no se puede equivocar.

    Si dos personas compartieran el `conversacion`, compartirían la memoria:
    una leería la conversación de la otra.
    """
    canal = TelegramQueTermina(
        tandas=[
            [novedad("hola", chat=111, update=1)],
            [novedad("hola", chat=222, update=2)],
        ]
    )
    agente = agente_falso(["para el primero", "para el segundo"])

    with pytest.raises(KeyboardInterrupt):
        atender(agente, canal)

    assert len(agente.historial("111")) == 2
    assert len(agente.historial("222")) == 2
    assert agente.historial("333") == [], "un chat que no escribió no tiene nada"


def test_si_el_modelo_falla_el_bot_no_se_cae():
    """Un error con una persona no puede dejar sin atender a las demás."""

    canal = TelegramQueTermina(tandas=[[novedad("hola", chat=555, update=1)]])
    agente = agente_falso([])  # sin respuestas: el modelo falso revienta

    with pytest.raises(KeyboardInterrupt):  # llegó al final, no murió antes
        atender(agente, canal)

    enviados = [e for e in canal.enviados if e["metodo"] == "sendMessage"]
    assert len(enviados) == 1, "le tiene que avisar a la persona"
    assert "rompió" in enviados[0]["datos"]["text"]
