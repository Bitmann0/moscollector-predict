"""Объектный контекст: 86% отказов в данных групповые, поэтому состояние
объекта — сильнейший внешний предиктор для отдельного канала.
"""
import duckdb
import pytest

from mkl.features import relative


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE feat_spatial (
        ch BIGINT, day DATE, obj VARCHAR, obj_parent VARCHAR,
        stype VARCHAR, picket DOUBLE,
        n_events BIGINT, n_alarms BIGINT, n_bad BIGINT,
        n_bad_w7 BIGINT, n_bad_w30 BIGINT, n_alarms_w7 BIGINT,
        silence_z DOUBLE, days_since_last_bad BIGINT)""")
    yield c
    c.close()


def _row(c, ch, day, obj, n_bad=0, n_alarms=0, n_events=1):
    c.execute("INSERT INTO feat_spatial VALUES "
              "(?,?,?,?,'Датчик дыма',1.0,?,?,?,?,0,?,0.0,1)",
              [ch, day, obj, obj, n_events, n_alarms, n_bad, n_bad, n_alarms])


def test_object_context_counts_sibling_channels(con):
    _row(con, 1, "2025-01-01", "A", n_bad=0)
    _row(con, 2, "2025-01-01", "A", n_bad=3)
    _row(con, 3, "2025-01-01", "A", n_bad=5)
    relative.add_object_context(con)
    got = dict(con.execute(
        "SELECT ch, obj_n_bad FROM feat_objctx"
    ).fetchall())
    assert all(v == 8 for v in got.values()), "объектная сумма одинакова для всех каналов"


def test_frac_bad_reflects_object_health(con):
    _row(con, 1, "2025-01-01", "A", n_bad=1)
    _row(con, 2, "2025-01-01", "A", n_bad=0)
    _row(con, 3, "2025-01-01", "A", n_bad=0)
    _row(con, 4, "2025-01-01", "A", n_bad=0)
    relative.add_object_context(con)
    v = con.execute("SELECT DISTINCT obj_frac_bad FROM feat_objctx").fetchone()[0]
    assert v == pytest.approx(0.25)


def test_objects_do_not_leak_into_each_other(con):
    _row(con, 1, "2025-01-01", "A", n_bad=10)
    _row(con, 2, "2025-01-01", "B", n_bad=0)
    relative.add_object_context(con)
    got = dict(con.execute("SELECT ch, obj_n_bad FROM feat_objctx").fetchall())
    assert got[1] == 10 and got[2] == 0


def test_share_of_object_alarms(con):
    _row(con, 1, "2025-01-01", "A", n_alarms=75)
    _row(con, 2, "2025-01-01", "A", n_alarms=25)
    relative.add_object_context(con)
    got = dict(con.execute("SELECT ch, share_obj_alarms FROM feat_objctx").fetchall())
    assert got[1] == pytest.approx(0.75)
    assert got[2] == pytest.approx(0.25)


def test_context_is_same_day_only(con):
    _row(con, 1, "2025-01-01", "A", n_bad=0)
    _row(con, 2, "2025-01-02", "A", n_bad=99)
    relative.add_object_context(con)
    v = con.execute(
        "SELECT obj_n_bad FROM feat_objctx WHERE day = DATE '2025-01-01'"
    ).fetchone()[0]
    assert v == 0, "контекст не должен тянуть будущие сутки"
