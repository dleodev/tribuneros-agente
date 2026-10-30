"""El historial de cambios de stock: quién cambió qué, cuándo, y de cuánto a cuánto.

Sirve para dos cosas: saber qué pasó, y deshacer un guardado completo.
Vive en Postgres (la misma instancia del agente, tablas con prefijo
`mayorista_`). Sin POSTGRES_DSN el panel no se habilita: un historial que no
se guarda no sirve para deshacer.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

DSN = os.getenv("POSTGRES_DSN", "").strip()

CREAR_TABLA = """
CREATE TABLE IF NOT EXISTS mayorista_historial (
    id          BIGSERIAL PRIMARY KEY,
    lote        TEXT        NOT NULL,
    fecha       TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario     TEXT        NOT NULL,
    accion      TEXT        NOT NULL,
    producto_id BIGINT      NOT NULL,
    variante_id BIGINT,
    producto    TEXT        NOT NULL,
    talle       TEXT,
    antes       INTEGER,
    despues     INTEGER,
    deshecho    BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS mayorista_historial_lote ON mayorista_historial (lote);
"""

REVERSIBLES = ("stock", "talle_nuevo")  # lo que "deshacer" sabe revertir

CAMPOS = ("id", "lote", "fecha", "usuario", "accion", "producto_id", "variante_id", "producto", "talle", "antes", "despues", "deshecho")


def lote_nuevo() -> str:
    return uuid.uuid4().hex[:12]


class HistorialMemoria:
    """Para las pruebas: lo mismo, sin base."""

    def __init__(self) -> None:
        self.filas: list[dict] = []

    def preparar(self) -> None:
        pass

    def anotar(self, lote: str, usuario: str, entradas: list[dict]) -> None:
        for e in entradas:
            self.filas.append(
                {
                    "id": len(self.filas) + 1,
                    "lote": lote,
                    "fecha": datetime.now(timezone.utc).isoformat(),
                    "usuario": usuario,
                    "deshecho": False,
                    **{k: e.get(k) for k in CAMPOS[4:11]},
                }
            )

    def lotes(self, limite: int = 30) -> list[dict]:
        return _agrupar(sorted(self.filas, key=lambda f: -f["id"]), limite)

    def entradas_del_lote(self, lote: str) -> list[dict]:
        return [f for f in self.filas if f["lote"] == lote]

    def marcar_deshecho(self, lote: str) -> None:
        for f in self.filas:
            if f["lote"] == lote and f["accion"] in REVERSIBLES:
                f["deshecho"] = True


class HistorialPG:
    """El historial de verdad, en Postgres."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._candado = threading.Lock()

    def _conectar(self):
        import psycopg

        return psycopg.connect(self.dsn, autocommit=True)

    def preparar(self) -> None:
        with self._conectar() as c:
            c.execute(CREAR_TABLA)

    def anotar(self, lote: str, usuario: str, entradas: list[dict]) -> None:
        if not entradas:
            return
        with self._conectar() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO mayorista_historial (lote, usuario, accion, producto_id, variante_id, producto, talle, antes, despues) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (lote, usuario, e["accion"], e["producto_id"], e.get("variante_id"), e["producto"], e.get("talle"), e.get("antes"), e.get("despues"))
                    for e in entradas
                ],
            )

    def _filas(self, donde: str = "", parametros: tuple = (), limite: int | None = None) -> list[dict]:
        sql = f"SELECT {', '.join(CAMPOS)} FROM mayorista_historial {donde} ORDER BY id DESC"
        if limite:
            sql += f" LIMIT {int(limite)}"
        with self._conectar() as c, c.cursor() as cur:
            cur.execute(sql, parametros)
            return [dict(zip(CAMPOS, f)) | {"fecha": f[2].isoformat()} for f in cur.fetchall()]

    def lotes(self, limite: int = 30) -> list[dict]:
        return _agrupar(self._filas(limite=limite * 60), limite)

    def entradas_del_lote(self, lote: str) -> list[dict]:
        return self._filas("WHERE lote = %s", (lote,))

    def marcar_deshecho(self, lote: str) -> None:
        with self._conectar() as c:
            c.execute("UPDATE mayorista_historial SET deshecho = TRUE WHERE lote = %s AND accion = ANY(%s)", (lote, list(REVERSIBLES)))


def _agrupar(filas: list[dict], limite: int) -> list[dict]:
    """Las filas sueltas, juntas por guardado (lote), del más nuevo al más viejo."""
    lotes: dict[str, dict] = {}
    for f in filas:
        lote = lotes.setdefault(
            f["lote"],
            {"lote": f["lote"], "fecha": f["fecha"], "usuario": f["usuario"], "entradas": [], "deshecho": True},
        )
        lote["entradas"].append(f)
        if f["accion"] in REVERSIBLES and not f["deshecho"]:
            lote["deshecho"] = False
    salida = list(lotes.values())[:limite]
    for lote in salida:
        # Un lote que es solo el alta de un modelo nuevo no se puede deshacer desde acá.
        lote["se_puede_deshacer"] = any(e["accion"] in REVERSIBLES for e in lote["entradas"]) and not lote["deshecho"]
    return salida


def crear() -> HistorialPG | None:
    return HistorialPG(DSN) if DSN else None
