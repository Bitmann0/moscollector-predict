"""Эпизодная метрика: подряд идущие положительные сутки — одно событие.

Посуточный счёт завышает качество тем сильнее, чем длиннее отказы: канал,
лежащий месяц, иначе засчитывается тридцатью попаданиями вместо одного.
"""
import numpy as np
import pytest

from mkl import metrics


def _mk(rows):
    """rows: (entity, 'YYYY-MM-DD', y, p)"""
    e = np.array([r[0] for r in rows])
    d = np.array([r[1] for r in rows], dtype="datetime64[D]")
    y = np.array([r[2] for r in rows])
    p = np.array([r[3] for r in rows], dtype=float)
    return e, d, y, p


def test_consecutive_positive_days_count_as_one_episode():
    rows = [("a", f"2025-01-{d:02d}", 1, 0.9) for d in range(1, 11)]
    out = metrics.episode_summary(*_mk(rows))
    assert out["episodes"] == 1
    assert out["days_per_episode"] == pytest.approx(10.0)


def test_gap_splits_episodes():
    rows = ([("a", f"2025-01-{d:02d}", 1, 0.9) for d in (1, 2, 3)]
            + [("a", "2025-01-04", 0, 0.1)]
            + [("a", f"2025-01-{d:02d}", 1, 0.9) for d in (5, 6)])
    assert metrics.episode_summary(*_mk(rows))["episodes"] == 2


def test_different_entities_do_not_merge():
    rows = [("a", "2025-01-01", 1, 0.9), ("b", "2025-01-02", 1, 0.9)]
    assert metrics.episode_summary(*_mk(rows))["episodes"] == 2


def test_episode_caught_by_alert_on_preceding_day():
    """Алерт накануне начала эпизода засчитывается: это и есть прогноз."""
    rows = [("a", "2025-01-01", 0, 0.99), ("a", "2025-01-02", 1, 0.01),
            ("a", "2025-01-03", 1, 0.01)]
    rows += [("b", f"2025-02-{d:02d}", 0, 0.0) for d in range(1, 21)]
    out = metrics.episode_summary(*_mk(rows), horizon_days=1)
    assert out["episode_recall"] == pytest.approx(1.0)


def test_episode_missed_when_alert_too_early():
    rows = [("a", "2025-01-01", 0, 0.99)] + [("a", f"2025-01-{d:02d}", 0, 0.0)
                                             for d in (2, 3)]
    rows += [("a", "2025-01-04", 1, 0.0)]
    rows += [("b", f"2025-02-{d:02d}", 0, 0.5) for d in range(1, 21)]
    out = metrics.episode_summary(*_mk(rows), horizon_days=1)
    assert out["episode_recall"] == pytest.approx(0.0)


def test_long_episode_does_not_inflate_recall():
    """Один длинный эпизод и один короткий весят одинаково."""
    long_ep = [("a", f"2025-01-{d:02d}", 1, 0.99) for d in range(2, 30)]
    long_ep = [("a", "2025-01-01", 0, 0.99)] + long_ep
    short_ep = [("b", "2025-01-01", 0, 0.0), ("b", "2025-01-02", 1, 0.0)]
    filler = [("c", f"2025-03-{d:02d}", 0, 0.0) for d in range(1, 29)]
    out = metrics.episode_summary(*_mk(long_ep + short_ep + filler), horizon_days=1)
    assert out["episodes"] == 2
    assert out["episode_recall"] == pytest.approx(0.5), \
        "длинный эпизод пойман, короткий нет — ровно половина, а не 28/29"


def test_no_positives_returns_nan():
    rows = [("a", "2025-01-01", 0, 0.5)]
    out = metrics.episode_summary(*_mk(rows))
    assert out["episodes"] == 0


def test_episode_budget_comes_from_outside_not_from_the_labels():
    """Бюджет внутри метрики брался как 0.7 от числа эпизодов В ТЕСТЕ, то есть
    из меток: метрика зависела от того, сколько отказов случилось, и между
    фолдами разного размера была несопоставима."""
    ent = np.array(["a"] * 10)
    day = np.array([f"2025-01-{d:02d}" for d in range(1, 11)], dtype="datetime64[D]")
    y = np.array([1, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    p = np.linspace(1.0, 0.1, 10)
    assert metrics.episode_summary(ent, day, y, p, budget=3)["episode_budget"] == 3
    assert metrics.episode_summary(ent, day, y, p, budget=7)["episode_budget"] == 7


def test_random_score_does_not_score_like_a_model():
    """Проверка на фальсифицируемость. Эпизодные метрики с зачётом «попал хотя
    бы раз» известны тем, что случайный скор на них выглядит прилично: на SWaT
    такая логика даёт F1 0.969. Если наша метрика не отличает случайный скор от
    информативного, отбирать по ней рычаги нельзя.
    """
    rng = np.random.default_rng(0)
    n_ch, n_d = 60, 60
    ent = np.repeat([f"c{i}" for i in range(n_ch)], n_d)
    day = np.tile(np.arange("2025-01-01", "2025-03-02", dtype="datetime64[D]")[:n_d], n_ch)
    y = (rng.random(n_ch * n_d) < 0.05).astype(int)
    budget = 60
    rnd = metrics.episode_summary(ent, day, y, rng.random(n_ch * n_d), budget=budget)
    good = metrics.episode_summary(ent, day, y, y * 0.9 + rng.random(n_ch * n_d) * 0.1,
                                   budget=budget)
    assert good["episode_recall"] > 3 * rnd["episode_recall"], (
        f"информативный скор {good['episode_recall']:.3f} против случайного "
        f"{rnd['episode_recall']:.3f} — метрика не различает модели")
