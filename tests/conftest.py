"""Общие фикстуры.

Схема суточной панели выводится из самой продакшн-функции, а не переписывается
в каждом тесте руками: за время работы панель трижды прирастала колонками, и
каждый раз половина тестов падала из-за расхождения фикстуры со схемой.
"""
import duckdb
import pytest

from mkl import panel

EV_SCHEMA = """
    CREATE TABLE ev (event_id BIGINT, ch BIGINT, ts TIMESTAMP, day DATE,
                     alarm BOOLEAN, val_raw VARCHAR, val_num DOUBLE,
                     sys VARCHAR, stype VARCHAR, tag VARCHAR, sname VARCHAR,
                     obj VARCHAR, picket DOUBLE)
"""


def _panel_schema() -> str:
    """DDL суточной панели, полученный прогоном продакшн-запроса вхолостую."""
    c = duckdb.connect(":memory:")
    c.execute(EV_SCHEMA)
    panel.build_daily_channel(c)
    cols = ", ".join(f"{r[0]} {r[1]}"
                     for r in c.execute("DESCRIBE daily_channel").fetchall())
    c.close()
    return f"CREATE TABLE daily_channel ({cols})"


PANEL_SCHEMA = _panel_schema()


@pytest.fixture
def daily_con():
    """Пустая суточная панель со схемой, совпадающей с продакшн-функцией."""
    c = duckdb.connect(":memory:")
    c.execute(PANEL_SCHEMA)
    yield c
    c.close()


def insert_day(con, ch=1, day="2025-01-01", obj="A", stype="Датчик дыма",
               picket=1.0, **counts):
    """Строка панели с явными именами колонок.

    Позиционный INSERT ломается при каждом росте схемы; именованный — нет.
    """
    base = {"ch": ch, "day": day, "obj": obj, "stype": stype, "sys": "s",
            "picket": picket}
    base.update(counts)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    con.execute(f"INSERT INTO daily_channel ({cols}) VALUES ({ph})",
                list(base.values()))
