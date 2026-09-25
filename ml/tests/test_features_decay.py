"""Затухающая интенсивность.

Прямоугольное окно 7/30 взвешивает вчерашний всплеск и всплеск шестидневной
давности одинаково, а на седьмые сутки обнуляет его разом.
"""
import duckdb
import pytest

from mkl.features import decay


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE daily_channel (
        ch BIGINT, day DATE, n_events BIGINT, n_alarms BIGINT, n_bad BIGINT)""")
    yield c
    c.close()


def _row(c, ch, day, n_events=0):
    c.execute("INSERT INTO daily_channel VALUES (?,?,?,0,0)", [ch, day, n_events])


def _get(c, col):
    decay.add_decayed_intensity(c)
    return c.execute(f"SELECT day, {col} FROM feat_decay ORDER BY day").fetchall()


def test_single_event_decays_by_half_over_the_half_life(con):
    _row(con, 1, "2025-01-01", n_events=100)
    for d in range(2, 10):
        _row(con, 1, f"2025-01-{d:02d}")
    got = dict((str(r[0]), r[1]) for r in _get(con, "ewma_n_events_hl7"))
    assert got["2025-01-01"] == pytest.approx(100.0)
    assert got["2025-01-08"] == pytest.approx(50.0, rel=1e-6), "ровно период полураспада"


def test_decay_counts_days_not_rows(con):
    """Строки есть только у отчётных суток. Порядковое затухание считало бы
    месячный простой одним шагом."""
    _row(con, 1, "2025-01-01", n_events=100)
    _row(con, 1, "2025-02-01")
    got = dict((str(r[0]), r[1]) for r in _get(con, "ewma_n_events_hl7"))
    # 31 сутки — 4.43 периода полураспада: 100 * 0.5**(31/7) = 4.64.
    # Порядковое затухание увидело бы здесь один шаг и оставило бы 90.6.
    assert got["2025-02-01"] == pytest.approx(100 * 0.5 ** (31 / 7), rel=1e-6)
    assert got["2025-02-01"] < 10, "а не 90.6, как при счёте по строкам"


def test_channels_do_not_leak_into_each_other(con):
    _row(con, 1, "2025-01-01", n_events=100)
    _row(con, 2, "2025-01-01", n_events=0)
    decay.add_decayed_intensity(con)
    got = dict(con.execute(
        "SELECT ch, ewma_n_events_hl7 FROM feat_decay").fetchall())
    assert got[1] == pytest.approx(100.0) and got[2] == pytest.approx(0.0)


def test_ratio_is_high_right_after_a_spike_and_falls_later(con):
    _row(con, 1, "2025-01-01", n_events=10)
    for d in range(2, 16):
        _row(con, 1, f"2025-01-{d:02d}", n_events=10)
    _row(con, 1, "2025-01-16", n_events=200)
    for d in range(17, 31):
        _row(con, 1, f"2025-01-{d:02d}", n_events=10)
    got = dict((str(r[0]), r[1]) for r in _get(con, "ewma_n_events_r1_7"))
    assert got["2025-01-16"] > got["2025-01-15"], "всплеск виден сразу"
    assert got["2025-01-30"] < got["2025-01-16"], "и забывается"


def test_no_future_leak(con):
    """Значение суток t не зависит от того, что будет после t."""
    for d in range(1, 6):
        _row(con, 1, f"2025-01-{d:02d}", n_events=5)
    before = dict((str(r[0]), r[1]) for r in _get(con, "ewma_n_events_hl7"))
    _row(con, 1, "2025-01-06", n_events=9999)
    after = dict((str(r[0]), r[1]) for r in _get(con, "ewma_n_events_hl7"))
    assert before["2025-01-05"] == pytest.approx(after["2025-01-05"])
