"""Подавление повторов и подтверждение в суточной выдаче.

Отсечка по бюджету состояния не имеет: канал, лежащий месяц, занимает место в
выдаче каждые сутки. По эпизодной постановке это одно событие, и тридцать
выездов к нему — один пойманный отказ, а не тридцать.
"""
import numpy as np
import polars as pl

from mkl import metrics, serve


def _frame(rows):
    """rows: (ch, day_index, risk)"""
    return pl.DataFrame({
        "ch": [r[0] for r in rows],
        "day": [f"2025-01-{r[1]:02d}" for r in rows],
        "risk": [r[2] for r in rows],
    }).with_columns(pl.col("day").str.to_date())


def test_cooldown_suppresses_the_same_entity_next_day():
    df = _frame([(1, d, 0.9) for d in range(1, 6)]
                + [(2, d, 0.1) for d in range(1, 6)])
    out = serve.alerts_over_time(df, budget_per_day=1, entity="ch", cooldown_days=2)
    got = out.filter(pl.col("alert")).select(["ch", "day"]).sort("day")
    assert got["ch"].to_list() == [1, 1], "второй алерт только после паузы"
    assert [str(d) for d in got["day"]] == ["2025-01-01", "2025-01-04"]


def test_without_cooldown_the_same_entity_takes_the_budget_every_day():
    df = _frame([(1, d, 0.9) for d in range(1, 6)]
                + [(2, d, 0.1) for d in range(1, 6)])
    out = serve.alerts_over_time(df, budget_per_day=1, entity="ch", cooldown_days=0)
    assert out.filter(pl.col("alert")).height == 5


def test_confirmation_ignores_a_single_spike():
    """Одиночный всплеск риска чаще шум, чем начало отказа."""
    # Канал 2 в верхушке всегда, канал 1 выстреливает ровно один раз.
    rows = [(2, d, 0.5) for d in range(1, 7)]
    rows += [(1, d, 0.95 if d == 3 else 0.1) for d in range(1, 7)]
    out = serve.alerts_over_time(_frame(rows), budget_per_day=1, entity="ch",
                                 confirm_of_3=True)
    got = out.filter(pl.col("alert"))
    assert 1 not in got["ch"].to_list(), "одиночный всплеск не подтверждён"


def test_confirmation_lets_through_a_sustained_rise():
    rows = [(1, d, 0.9 if d >= 3 else 0.1) for d in range(1, 7)]
    rows += [(2, d, 0.5) for d in range(1, 7)]
    out = serve.alerts_over_time(_frame(rows), budget_per_day=1, entity="ch",
                                 confirm_of_3=True)
    days = sorted(str(d) for d in out.filter(pl.col("alert"))["day"].to_list())
    assert "2025-01-04" in days, "на вторые сутки роста подтверждение набрано"


# --- эпизодная эффективность -------------------------------------------------

def _ep(ent, day, y, alert):
    return metrics.episodes_per_100_alerts(
        np.array(ent), np.array(day, dtype="datetime64[D]"),
        np.array(y), np.array(alert))


def test_thirty_alerts_on_one_episode_count_as_one_catch():
    n = 30
    got = _ep(["a"] * n, [f"2025-01-{d:02d}" for d in range(1, n + 1)],
              [1] * n, [True] * n)
    assert got["episodes"] == 1
    assert got["episodes_caught"] == 1
    assert got["episodes_per_100_alerts"] < 4, "тридцать выездов к одному отказу"


def test_one_alert_per_distinct_episode_scores_high():
    ent = ["a", "a", "b", "b"]
    day = ["2025-01-01", "2025-01-05", "2025-01-01", "2025-01-05"]
    got = _ep(ent, day, [1, 1, 1, 1], [True, True, True, True])
    assert got["episodes"] == 4 and got["episodes_caught"] == 4
    assert got["episodes_per_100_alerts"] == 100.0


def test_no_alerts_gives_nan_not_division_error():
    got = _ep(["a"], ["2025-01-01"], [1], [False])
    assert got["alerts"] == 0 and got["episodes_per_100_alerts"] != got["episodes_per_100_alerts"]
