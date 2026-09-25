"""Признаки числовых каналов: флатлайн, чистка переполнений, отклонения газа.

Газ даёт 259 замеров на канал в сутки, из которых 93% — ровно 0.0, поэтому
суточные min/max/mean/std физически не выражают сигнал: он в том, когда и
насколько случаются отклонения. Залипший датчик выражается длиной прогона
одинаковых значений, а не разбросом.
"""
import duckdb
import pytest

from conftest import EV_SCHEMA, insert_event
from mkl import panel


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(EV_SCHEMA)
    yield c
    c.close()


def _ins(c, rows, stype="Газовый датчик"):
    for i, (ts, val) in enumerate(rows):
        num = None
        try:
            num = float(val)
        except ValueError:
            pass
        insert_event(c, i, 1, ts, val, val_num=num, stype=stype)


def test_flatline_run_detects_stuck_sensor(con):
    _ins(con, [(f"2025-01-01 10:{m:02d}:00", "0.00") for m in range(10)])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT max_flat_run, n_distinct_vals, val_std FROM daily_channel"
    ).fetchone()
    assert row[0] == 10, "десять одинаковых подряд — прогон длиной 10"
    assert row[1] == 1
    assert row[2] == pytest.approx(0.0), "разброс нулевой и залипание им не выражается"


def test_flatline_run_shorter_when_values_vary(con):
    _ins(con, [("2025-01-01 10:00:00", "0.00"), ("2025-01-01 10:01:00", "0.00"),
               ("2025-01-01 10:02:00", "0.10"), ("2025-01-01 10:03:00", "0.00")])
    panel.build_daily_channel(con)
    assert con.execute("SELECT max_flat_run FROM daily_channel").fetchone()[0] == 2


def test_temperature_sentinels_are_excluded(con):
    _ins(con, [("2025-01-01 10:00:00", "-3276"), ("2025-01-01 11:00:00", "999"),
               ("2025-01-01 12:00:00", "21.5")], stype="Датчик температуры")
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT val_min, val_ok_min, val_ok_max, n_val_ok, n_val_bad FROM daily_channel"
    ).fetchone()
    assert row[0] == pytest.approx(-3276.0), "сырое поле остаётся как было"
    assert row[1] == pytest.approx(21.5) and row[2] == pytest.approx(21.5)
    assert row[3] == 1 and row[4] == 2


def test_gas_saturation_is_flagged_and_excluded(con):
    _ins(con, [("2025-01-01 10:00:00", "327.68"), ("2025-01-01 11:00:00", "0.02")])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT n_saturated, n_val_ok, val_ok_max FROM daily_channel"
    ).fetchone()
    assert row[0] == 1 and row[1] == 1
    assert row[2] == pytest.approx(0.02)


def test_gas_excursion_counters(con):
    _ins(con, [("2025-01-01 10:00:00", "0.00"), ("2025-01-01 10:01:00", "0.03"),
               ("2025-01-01 10:02:00", "0.10"), ("2025-01-01 10:03:00", "0.50")])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT n_val_nonzero, n_val_gt005, n_val_gt02 FROM daily_channel"
    ).fetchone()
    assert row == (3, 2, 1)
