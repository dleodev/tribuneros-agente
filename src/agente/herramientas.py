"""Las herramientas: lo que el agente puede hacer además de conversar.

Una herramienta es una función común de Python que el modelo puede decidir
llamar. El modelo no la ejecuta: dice "quiero llamar a clima con lugar=Rosario"
y LangGraph la corre y le devuelve el resultado. Por eso el **docstring importa
tanto como el código**: es literalmente lo único que el modelo lee para decidir
si esta herramienta le sirve y qué mandarle.

Hay dos grupos:

- **El clima**, con Open-Meteo (https://open-meteo.com): gratis, sin clave.
- **La tienda** (Tiendanube), con `buscar_producto` y `consultar_pedido`: leen
  la tienda real de Tribuneros. Solo hacen falta si están cargadas
  TIENDANUBE_STORE_ID y TIENDANUBE_ACCESS_TOKEN — si no, esas dos herramientas
  igual están en la lista, pero avisan que la tienda no está conectada en vez
  de romper.

Todas usan `urllib`, de la biblioteca estándar, para no sumar dependencias al
requirements.txt por unos pedidos HTTP.
"""

from __future__ import annotations

import difflib
import json
import os
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from langchain_core.tools import tool

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
PRONOSTICO = "https://api.open-meteo.com/v1/forecast"

# Si Open-Meteo no contesta en este tiempo, cortamos. Sin un tope, una consulta
# colgada deja al agente mudo en la mitad de la conversación.
ESPERA = 15

# Open-Meteo devuelve el estado del cielo como un número (el código WMO, un
# estándar meteorológico). Esta es la traducción a algo que una persona lea.
CIELO = {
    0: "despejado",
    1: "casi despejado",
    2: "parcialmente nublado",
    3: "nublado",
    45: "con niebla",
    48: "con niebla que escarcha",
    51: "con llovizna suave",
    53: "con llovizna",
    55: "con llovizna fuerte",
    56: "con llovizna helada",
    57: "con llovizna helada fuerte",
    61: "con lluvia suave",
    63: "con lluvia",
    65: "con lluvia fuerte",
    66: "con lluvia helada",
    67: "con lluvia helada fuerte",
    71: "con nevada suave",
    73: "con nevada",
    75: "con nevada fuerte",
    77: "con granizo fino",
    80: "con chaparrones",
    81: "con chaparrones fuertes",
    82: "con chaparrones muy fuertes",
    85: "con chaparrones de nieve",
    86: "con chaparrones de nieve fuertes",
    95: "con tormenta",
    96: "con tormenta y algo de granizo",
    99: "con tormenta y granizo",
}


@tool
def clima(lugar: str) -> str:
    """Dice el clima que hace ahora mismo en una ciudad.

    Usala cuando te pregunten por el clima, la temperatura, si llueve, si hace
    frío o calor, o si conviene salir con abrigo o paraguas.

    Args:
        lugar: La ciudad, en lo posible con el país. Por ejemplo "Rosario",
            "Buenos Aires, Argentina" o "Madrid". Si hay varias ciudades con
            el mismo nombre, se toma la más conocida.
    """
    # Por qué la herramienta devuelve el error en vez de levantarlo (esta es la
    # cuarta falla que se traga el proyecto, y va con el motivo escrito al lado,
    # como pide AGENTS.md): si una herramienta explota, LangGraph corta toda la
    # respuesta y la persona ve un error crudo. Devolviéndolo como texto, el
    # resultado le llega al modelo, que lo cuenta con sus palabras y la
    # conversación sigue. Ojo con la diferencia: acá no se esconde nada, el
    # problema igual termina en la pantalla — pero explicado y sin voltear la
    # charla. Los errores del *proveedor* siguen saliendo tal cual: esto es
    # una consulta a Open-Meteo, no al modelo.
    try:
        encontrado = _buscar_lugar(lugar)
    except Exception as e:
        return f"No se pudo consultar el clima de '{lugar}': {type(e).__name__}: {e}"

    if encontrado is None:
        return (
            f"No encontré ninguna ciudad que se llame '{lugar}'. "
            "Puede estar mal escrita, o convenir agregarle el país."
        )

    try:
        datos = _pedir_el_clima(encontrado["latitud"], encontrado["longitud"])
    except Exception as e:
        return f"No se pudo consultar el clima de '{lugar}': {type(e).__name__}: {e}"

    return _redactar(encontrado, datos)


@tool
def buscar_producto(consulta: str) -> str:
    """Busca productos reales de la tienda: precio, talles con stock y el link.

    Usala cuando alguien pregunte por una camiseta o producto, si hay stock,
    cuánto sale, o pida una recomendación. Buscá con las palabras que usó la
    persona tal cual salieron, aunque estén mal escritas o incompletas (por
    ejemplo "camiseta argentna" o "boca titular 25") — esta herramienta
    tolera bastante el error de tipeo sola, así que no hace falta corregir
    nada antes de llamarla.

    Si el resultado viene sin stock, la respuesta ya trae alternativas con
    stock — ofrecelas antes de que la persona pregunte de nuevo.

    Args:
        consulta: lo que la persona busca, en sus propias palabras.
    """
    if not _tiendanube_configurado():
        return "La tienda todavía no está conectada a este agente."

    try:
        productos = _buscar_en_catalogo(consulta)
    except Exception as e:
        return f"No se pudo consultar la tienda: {type(e).__name__}: {e}"

    if not productos:
        return (
            f"No encontré ningún producto que coincida con '{consulta}'. "
            "Puede estar mal escrito o no ser algo que vendamos."
        )

    return "\n\n".join(_describir_producto(p) for p in productos)


@tool
def consultar_pedido(nombre: str, correo: str, numero_orden: str) -> str:
    """Consulta el estado de un pedido ya hecho: envío, seguimiento, en qué va.

    Usala solo para pedidos YA HECHOS (no para elegir qué comprar, para eso
    está buscar_producto). Antes de llamarla, pedile a la persona los tres
    datos — nombre completo, correo, y número de orden (el que le llegó por
    mail al comprar) — y no llames a la herramienta hasta tener los tres.

    El número de orden es la clave de todo: sin el número correcto no se
    entrega ninguna información, aunque el nombre y el correo sean
    perfectos. Si la herramienta dice que no pudo verificar el pedido, no
    inventes ni supongas nada — pedile que revise los datos, o derivá a una
    persona del equipo si insiste en que están bien.

    Args:
        nombre: nombre completo que dio la persona.
        correo: el correo que dio la persona.
        numero_orden: el número de orden que dio la persona (solo el número,
            sin el "#").
    """
    if not _tiendanube_configurado():
        return "La tienda todavía no está conectada a este agente."

    numero = "".join(c for c in numero_orden if c.isdigit())
    if not numero:
        return (
            "Ese número de orden no parece válido. Pedile que te pase el que "
            "le llegó por mail al comprar."
        )

    try:
        candidatos = _tiendanube_pedir("/orders", {"q": numero, "per_page": 10})
    except Exception as e:
        return f"No se pudo consultar el pedido: {type(e).__name__}: {e}"

    pedido = next((o for o in candidatos if str(o.get("number")) == numero), None)

    # Mismo mensaje si falla el número o si falla el nombre/correo: no hay
    # que darle a nadie una pista de cuál de los dos datos estuvo mal.
    if pedido is None or not _coincide_con_el_pedido(pedido, nombre, correo):
        return (
            "No pudimos verificar ese pedido con los datos que nos diste. "
            "Revisá que el nombre, el correo y el número de orden sean "
            "exactamente los de esa compra."
        )

    return _describir_pedido(pedido)


# Lo que el agente tiene atado. Cuando agregues otra herramienta, sumala acá:
# es la única lista que mira el grafo.
HERRAMIENTAS = [clima, buscar_producto, consultar_pedido]


# -- Las consultas ------------------------------------------------------------


def _buscar_lugar(nombre: str) -> dict | None:
    """Convierte un nombre de ciudad en coordenadas. None si no existe."""
    datos = _traer(
        GEOCODING,
        {"name": nombre, "count": 1, "language": "es", "format": "json"},
    )

    resultados = datos.get("results") or []
    if not resultados:
        return None

    primero = resultados[0]
    return {
        "nombre": primero.get("name") or nombre,
        # admin1 es la provincia o el estado. Sirve para desambiguar cuando hay
        # cinco ciudades con el mismo nombre en países distintos.
        "provincia": primero.get("admin1") or "",
        "pais": primero.get("country") or "",
        "latitud": primero["latitude"],
        "longitud": primero["longitude"],
    }


def _pedir_el_clima(latitud: float, longitud: float) -> dict:
    """El clima de este momento en esas coordenadas."""
    return _traer(
        PRONOSTICO,
        {
            "latitude": latitud,
            "longitude": longitud,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
            ),
            # timezone=auto hace que la hora venga en la del lugar consultado,
            # no en la nuestra. Si preguntás por Tokio querés la hora de Tokio.
            "timezone": "auto",
        },
    )


def _traer(url: str, parametros: dict) -> dict:
    """Un GET que devuelve JSON."""
    completa = f"{url}?{urllib.parse.urlencode(parametros)}"

    with urllib.request.urlopen(completa, timeout=ESPERA) as respuesta:
        return json.loads(respuesta.read().decode("utf-8"))


# -- El texto que lee el modelo -----------------------------------------------


def _redactar(lugar: dict, datos: dict) -> str:
    """Arma la respuesta en texto.

    Le devolvemos al modelo una frase escrita, no el JSON crudo: entiende
    cualquiera de los dos, pero con el texto ya redactado es mucho menos
    probable que se equivoque de unidad o invente un dato que no está.
    """
    ahora = datos.get("current") or {}

    partes = [f"Clima en {_nombre_completo(lugar)}:"]
    partes.append(f"- Temperatura: {ahora.get('temperature_2m', '?')} °C")

    sensacion = ahora.get("apparent_temperature")
    if sensacion is not None:
        partes.append(f"- Sensación térmica: {sensacion} °C")

    partes.append(f"- Cielo: {_describir_cielo(ahora.get('weather_code'))}")

    humedad = ahora.get("relative_humidity_2m")
    if humedad is not None:
        partes.append(f"- Humedad: {humedad} %")

    viento = ahora.get("wind_speed_10m")
    if viento is not None:
        partes.append(f"- Viento: {viento} km/h")

    hora = ahora.get("time")
    if hora:
        partes.append(f"- Medido a las {hora[11:16]}, hora local del lugar")

    return "\n".join(partes)


def _nombre_completo(lugar: dict) -> str:
    """"Rosario, Provincia de Santa Fe, Argentina" — sin comas de más."""
    return ", ".join(
        p for p in (lugar.get("nombre"), lugar.get("provincia"), lugar.get("pais")) if p
    )


def _describir_cielo(codigo) -> str:
    """El código WMO en castellano. Si es uno raro, devolvemos el número."""
    if codigo is None:
        return "sin datos"
    return CIELO.get(codigo, f"sin descripción (código {codigo})")


# -- Tiendanube -----------------------------------------------------------
#
# Solo lectura, a propósito: el token que se carga acá puede tener permisos
# de escritura (depende de cómo se creó la app en el panel de partners), pero
# estas funciones nunca hacen POST/PUT/DELETE. El agente asesora y consulta,
# nunca modifica un pedido ni un producto.

TIENDANUBE_BASE = "https://api.tiendanube.com/v1"

# Cómo se cuenta el estado de envío en criollo. Los valores de la izquierda
# son los que devuelve la API (ver la documentación de Tiendanube).
ENVIO = {
    "unpacked": "todavía estamos preparando tu pedido",
    "partially_packed": "estamos empaquetando tu pedido (una parte ya está lista)",
    "unshipped": "tu pedido ya está empaquetado, pronto lo enviamos",
    "partially_fulfilled": "una parte de tu pedido ya salió, el resto sigue en preparación",
    "shipped": "tu pedido ya está en camino",
    "delivered": "tu pedido ya fue entregado",
}

PAGO = {
    "paid": "está paga",
    "pending": "está pendiente de pago",
    "voided": "fue anulada",
    "refunded": "fue reembolsada",
    "partially_refunded": "fue reembolsada parcialmente",
    "abandoned": "quedó abandonada",
}


def _tiendanube_configurado() -> bool:
    return bool(os.getenv("TIENDANUBE_STORE_ID") and os.getenv("TIENDANUBE_ACCESS_TOKEN"))


def _tiendanube_pedir(ruta: str, parametros: dict) -> list | dict:
    """Un GET a la API de Tiendanube. `ruta` es relativa a /v1/<tienda>."""
    tienda = os.environ["TIENDANUBE_STORE_ID"]
    token = os.environ["TIENDANUBE_ACCESS_TOKEN"]
    url = f"{TIENDANUBE_BASE}/{tienda}{ruta}?{urllib.parse.urlencode(parametros)}"

    peticion = urllib.request.Request(url)
    # Tiendanube usa "Authentication", no "Authorization" — no es un typo.
    peticion.add_header("Authentication", f"bearer {token}")
    peticion.add_header("User-Agent", "Agente Tribuneros (leo@revoltia.cloud)")

    try:
        with urllib.request.urlopen(peticion, timeout=ESPERA) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No es un error de verdad: Tiendanube contesta 404 en vez de una
            # lista vacía cuando una búsqueda (ej. ?q=un-numero-que-no-existe)
            # no encuentra nada. Se trata igual que "sin resultados".
            return []
        raise


# Cuánto tiene que parecerse una palabra de la búsqueda a una palabra del
# producto para contar como "encontrado". Se ajustó a mano probando contra
# la tienda real: "argentna" (falta una i) tiene que encontrar "argentina".
UMBRAL_DE_PARECIDO = 0.72


def _catalogo_completo() -> list[dict]:
    """Todos los productos publicados, en un solo pedido.

    La tienda de Tribuneros tiene un catálogo chico (bien por debajo de 100
    productos), así que traerlo entero y comparar acá es más simple y más
    confiable que el buscador de Tiendanube, que no tolera errores de tipeo
    (se probó a mano: "argentna" ahí no encuentra nada).
    """
    return _tiendanube_pedir("/products", {"per_page": 100, "published": "true"})


def _buscar_en_catalogo(consulta: str, maximo: int = 5) -> list[dict]:
    """Los productos del catálogo que mejor matchean la consulta, ordenados."""
    catalogo = _catalogo_completo()

    puntuados = [(_puntaje(consulta, p), p) for p in catalogo]
    puntuados.sort(key=lambda par: par[0], reverse=True)

    return [p for puntaje, p in puntuados if puntaje >= UMBRAL_DE_PARECIDO][:maximo]


def _puntaje(consulta: str, producto: dict) -> float:
    """Qué tan bien matchea la consulta con este producto, de 0 a 1.

    Compara palabra por palabra (no la frase entera contra el texto entero):
    así "argentna titular" encuentra "Argentina 2026 titular" aunque el
    nombre real tenga de por medio "2026" y "(Versión Jugador)" que la
    consulta no mencionó.

    Mezcla el promedio con el mejor puntaje individual, no usa solo el
    promedio. Motivo real, no teórico: varios productos del catálogo (los de
    River, por ejemplo) no tienen ninguna etiqueta cargada, así que la
    palabra "camiseta" no matchea nada de ese producto — con el promedio
    solo, "camista river" se hundía por esa palabra suelta aunque "river"
    matcheara perfecto. El máximo evita que una palabra sin dónde pegar tape
    a otra que sí encontró justo lo que había que encontrar.
    """
    nombre = (producto.get("name") or {}).get("es") or ""
    etiquetas = producto.get("tags") or ""
    marca = producto.get("brand") or ""
    palabras_producto = _normalizar(f"{nombre} {etiquetas} {marca}").split()
    palabras_consulta = _normalizar(consulta).split()

    if not palabras_consulta or not palabras_producto:
        return 0.0

    puntajes = [
        max(
            difflib.SequenceMatcher(None, palabra, otra).ratio()
            for otra in palabras_producto
        )
        for palabra in palabras_consulta
    ]
    promedio = sum(puntajes) / len(puntajes)
    return (promedio + max(puntajes)) / 2


def _describir_producto(p: dict) -> str:
    """Nombre, precio, link y talles con stock — o alternativas si no hay."""
    nombre = (p.get("name") or {}).get("es") or "producto"
    link = p.get("canonical_url") or ""
    variantes = p.get("variants") or []

    precio = next(
        (v.get("promotional_price") or v.get("price") for v in variantes if v.get("price")),
        None,
    )

    lineas = [f"{nombre} — ${precio}" if precio else nombre]
    if link:
        lineas.append(f"Link: {link}")

    con_stock = [_talle(v) for v in variantes if (v.get("stock") or 0) > 0]
    if con_stock:
        lineas.append("Talles con stock: " + ", ".join(con_stock))
    else:
        lineas.append("Sin stock por ahora.")
        alternativas = _buscar_alternativas(p)
        if alternativas:
            lineas.append("Alternativas con stock: " + "; ".join(alternativas))

    return "\n".join(lineas)


def _talle(variante: dict) -> str:
    valores = variante.get("values") or []
    return ", ".join(v.get("es", "") for v in valores if v.get("es")) or "único"


def _buscar_alternativas(producto: dict, maximo: int = 3) -> list[str]:
    """Otros productos con stock, parecidos por nombre, etiquetas o marca."""
    nombre = (producto.get("name") or {}).get("es") or ""
    etiquetas = producto.get("tags") or ""
    marca = producto.get("brand") or ""

    try:
        catalogo = _catalogo_completo()
    except Exception:
        # Una alternativa que no se pudo buscar no tiene que voltear la
        # respuesta principal: el producto original ya se informó bien.
        return []

    candidatos = [
        c
        for c in catalogo
        if c.get("id") != producto.get("id") and c.get("has_stock")
    ]
    puntuados = [
        (_puntaje(f"{nombre} {etiquetas} {marca}", c), c) for c in candidatos
    ]
    puntuados.sort(key=lambda par: par[0], reverse=True)

    resultado = []
    for puntaje, c in puntuados:
        if puntaje < UMBRAL_DE_PARECIDO or len(resultado) >= maximo:
            break
        nombre_c = (c.get("name") or {}).get("es") or "producto"
        resultado.append(f"{nombre_c} ({c.get('canonical_url', '')})")

    return resultado


def _coincide_con_el_pedido(pedido: dict, nombre: str, correo: str) -> bool:
    """True si el correo coincide exacto, o si el nombre coincide razonablemente."""
    correo_pedido = (pedido.get("contact_email") or "").strip().lower()
    if correo_pedido and correo.strip().lower() == correo_pedido:
        return True

    nombre_pedido = _normalizar(pedido.get("contact_name") or "")
    nombre_dado = _normalizar(nombre)
    if nombre_pedido and nombre_dado and (
        nombre_dado in nombre_pedido or nombre_pedido in nombre_dado
    ):
        return True

    return False


def _normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, sin espacios de más — para comparar nombres."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tildes.lower().split())


def _describir_pedido(pedido: dict) -> str:
    numero = pedido.get("number")

    if pedido.get("status") == "cancelled":
        return f"Pedido #{numero}: está cancelado."

    lineas = [
        f"Pedido #{numero}: {ENVIO.get(pedido.get('shipping_status'), 'sin datos de envío')}."
    ]

    pago = pedido.get("payment_status")
    if pago:
        lineas.append(f"La compra {PAGO.get(pago, pago)}.")

    seguimiento = pedido.get("shipping_tracking_number")
    if seguimiento:
        transportista = pedido.get("shipping_carrier_name")
        extra = f" ({transportista})" if transportista else ""
        lineas.append(f"Número de seguimiento: {seguimiento}{extra}")

    url_seguimiento = pedido.get("shipping_tracking_url")
    if url_seguimiento:
        lineas.append(f"Podés rastrearlo acá: {url_seguimiento}")

    opcion = pedido.get("shipping_option")
    if opcion:
        lineas.append(f"Forma de envío: {opcion}")

    productos = pedido.get("products") or []
    if productos:
        # Acá, a diferencia del catálogo, "name" ya es un string plano: es
        # una foto de cómo se llamaba el producto al momento de la compra,
        # no el recurso completo de /products.
        nombres = [p.get("name") or "producto" for p in productos]
        lineas.append("Productos: " + ", ".join(nombres))

    return "\n".join(lineas)
