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
                     obj VARCHAR, obj_parent VARCHAR, obj_kind VARCHAR,
                     picket DOUBLE)
"""


@pytest.fixture
def ev_con():
    """Пустая таблица событий со схемой, совпадающей с ingest."""
    c = duckdb.connect(":memory:")
    c.execute(EV_SCHEMA)
    yield c
    c.close()


def insert_event(con, event_id, ch, ts, val_raw, alarm=False, val_num=None,
                 stype="Датчик дыма", obj="A", picket=1.0, sys="s"):
    """Событие с явными именами колонок: позиционный INSERT ломается
    при каждом росте схемы."""
    con.execute(
        "INSERT INTO ev (event_id, ch, ts, day, alarm, val_raw, val_num, sys,"
        " stype, tag, sname, obj, obj_parent, obj_kind, picket)"
        " VALUES (?,?,?,?,?,?,?,?,?,'t','n',?,?,'guardObject',?)",
        [event_id, ch, ts, ts[:10], alarm, val_raw, val_num, sys, stype,
         obj, obj, picket])


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
