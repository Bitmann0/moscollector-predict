"""Робастный остаток, относительное залипание и дрейф значений."""
import duckdb
import pytest

from mkl.features import telemetry


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE feat_base (
        ch BIGINT, day DATE, val_ok_med DOUBLE, n_val_ok BIGINT,
        max_flat_run BIGINT, n_distinct_vals BIGINT, n_val_nonzero BIGINT,
        n_val_bad BIGINT)""")
    yield c
    c.close()


def _day(c, day, med, flat=1, n_ok=100, distinct=5, nonzero=10, bad=0):
    c.execute("INSERT INTO feat_base VALUES (1,?,?,?,?,?,?,?)",
              [day, med, n_ok, flat, distinct, nonzero, bad])


def test_residual_is_zero_for_stable_channel(con):
    for d in range(1, 31):
        _day(con, f"2025-01-{d:02d}", 20.0)
    telemetry.add_value_features(con)
    v = con.execute(
        "SELECT val_resid FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v is None or v == pytest.approx(0.0)


def test_residual_flags_excursion(con):
    for d in range(1, 30):
        _day(con, f"2025-01-{d:02d}", 20.0 + (d % 3) * 0.5)
    _day(con, "2025-01-30", 40.0)
    telemetry.add_value_features(con)
    v = con.execute(
        "SELECT val_resid FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v > 10, "скачок на 20 при MAD порядка 0.5 — это десятки MAD"


def test_flat_run_ratio_is_relative_to_own_norm(con):
    """У газового канала прогон из сотни нулей норма, у температурного отказ."""
    for d in range(1, 31):
        _day(con, f"2025-01-{d:02d}", 0.0, flat=100)
    telemetry.add_value_features(con)
    v = con.execute(
        "SELECT flat_run_ratio FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v == pytest.approx(1.0), "привычное залипание не аномалия"


def test_flat_run_ratio_flags_new_sticking(con):
    for d in range(1, 30):
        _day(con, f"2025-01-{d:02d}", 20.0, flat=2)
    _day(con, "2025-01-30", 20.0, flat=200)
    telemetry.add_value_features(con)
    v = con.execute(
        "SELECT flat_run_ratio FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v > 50


def test_sampling_ratio_drops_when_channel_slows(con):
    for d in range(1, 30):
        _day(con, f"2025-01-{d:02d}", 20.0, n_ok=200)
    _day(con, "2025-01-30", 20.0, n_ok=10)
    telemetry.add_value_features(con)
    v = con.execute(
        "SELECT sampling_ratio FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v < 0.2


def test_drift_slope_is_positive_for_rising_channel(con):
    for d in range(1, 31):
        _day(con, f"2025-01-{d:02d}", 10.0 + d)
    telemetry.add_value_features(con)
    telemetry.add_value_drift(con)
    v = con.execute(
        "SELECT val_slope_w7 FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v == pytest.approx(1.0, abs=0.05), "рост на единицу в сутки"


def test_drift_slope_is_zero_for_flat_channel(con):
    for d in range(1, 31):
        _day(con, f"2025-01-{d:02d}", 20.0)
    telemetry.add_value_features(con)
    telemetry.add_value_drift(con)
    v = con.execute(
        "SELECT val_slope_w30 FROM feat_value WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v == pytest.approx(0.0, abs=1e-9)


def test_value_features_do_not_see_the_future():
    """Добавление будущих суток не меняет остаток прошлых."""
    def build(n_days):
        c = duckdb.connect(":memory:")
        c.execute("""CREATE TABLE feat_base (
            ch BIGINT, day DATE, val_ok_med DOUBLE, n_val_ok BIGINT,
            max_flat_run BIGINT, n_distinct_vals BIGINT, n_val_nonzero BIGINT,
            n_val_bad BIGINT)""")
        for d in range(1, n_days + 1):
            med = 20.0 if d <= 10 else 500.0
            c.execute("INSERT INTO feat_base VALUES (1,?,?,100,1,5,10,0)",
                      [f"2025-01-{d:02d}", med])
        telemetry.add_value_features(c)
        v = c.execute(
            "SELECT val_resid FROM feat_value WHERE day = DATE '2025-01-05'"
        ).fetchone()[0]
        c.close()
        return v

    assert build(10) == build(20)
