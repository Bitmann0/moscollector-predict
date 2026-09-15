import datetime as dt

import duckdb
import pytest

from conftest import PANEL_SCHEMA, insert_day
from mkl import store
from mkl.features import compute


def _daily_table(con):
    con.execute(PANEL_SCHEMA)


def _insert(con, day, n_events, ch=1, picket=10.0):
    insert_day(con, ch=ch, day=day, picket=picket, n_events=n_events)


def test_registry_lists_every_feature_column(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FEATURE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY", tmp_path / "features.yaml")
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE feat_ext AS SELECT 1 AS ch, DATE '2025-01-01' AS day, 2.0 AS n_events_w7"
    )
    store.write(con, "feat_ext", "sensor")
    reg = store.load_registry()
    assert "n_events_w7" in reg["sensor"]["columns"]
    assert reg["sensor"]["keys"] == ["ch", "day"]
    assert reg["sensor"]["n_rows"] == 1
    con.close()


def test_read_slice_respects_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FEATURE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY", tmp_path / "features.yaml")
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE feat_ext AS
        SELECT * FROM (VALUES (1, DATE '2025-01-01', 1.0), (1, DATE '2025-06-01', 2.0))
        AS t(ch, day, n_events_w7)""")
    store.write(con, "feat_ext", "sensor")
    got = store.read_slice("sensor", dt.date(2025, 1, 1), dt.date(2025, 1, 31))
    assert got.height == 1
    con.close()


def test_feature_value_does_not_change_when_future_rows_added():
    """Главный тест на утечку: добавление будущих суток не меняет фичу прошлых."""

    def build(days):
        con = duckdb.connect(":memory:")
        _daily_table(con)
        for d, n in days:
            _insert(con, d, n)
        compute.build_all(con, with_weather=False, with_episode_history=False)
        v = con.execute(
            "SELECT n_events_w7 FROM feat_ext WHERE day = DATE '2025-01-01'"
        ).fetchone()[0]
        con.close()
        return v

    short = build([("2025-01-01", 5)])
    long = build([("2025-01-01", 5), ("2025-01-02", 999), ("2025-01-03", 999)])
    assert short == long


def test_lifecycle_feature_does_not_change_when_future_rows_added():
    def build(days):
        con = duckdb.connect(":memory:")
        _daily_table(con)
        for d, n in days:
            _insert(con, d, n)
        compute.build_all(con, with_weather=False, with_episode_history=False)
        v = con.execute(
            "SELECT cum_events FROM feat_ext WHERE day = DATE '2025-01-01'"
        ).fetchone()[0]
        con.close()
        return v

    assert build([("2025-01-01", 5)]) == build([("2025-01-01", 5), ("2025-01-02", 999)])


def test_object_aggregation_sums_channels():
    con = duckdb.connect(":memory:")
    _daily_table(con)
    _insert(con, "2025-01-01", 10, ch=1, picket=10.0)
    _insert(con, "2025-01-01", 20, ch=2, picket=20.0)
    compute.build_all(con, with_weather=False, with_episode_history=False)
    compute.build_object_level(con)
    row = con.execute(
        "SELECT n_channels, n_events FROM feat_object"
    ).fetchone()
    assert row == (2, 30)
    con.close()


def test_segment_aggregation_splits_by_kilometre():
    con = duckdb.connect(":memory:")
    _daily_table(con)
    _insert(con, "2025-01-01", 10, ch=1, picket=5.0)
    _insert(con, "2025-01-01", 20, ch=2, picket=95.0)
    compute.build_all(con, with_weather=False, with_episode_history=False)
    compute.build_segment_level(con)
    got = dict(con.execute("SELECT seg, n_events FROM feat_segment").fetchall())
    assert got == {0: 10, 9: 20}
    con.close()
