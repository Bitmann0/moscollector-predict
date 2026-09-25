"""Стекинг и вес по свежести.

Главное требование к стекингу — строгая out-of-fold схема: вероятность,
посчитанная моделью, видевшей эти сутки при обучении, содержит ответ, и
любой прирост от такого признака фиктивен.
"""
import datetime as dt

import numpy as np
import polars as pl
import pytest

from mkl import cv, stacking


def _dataset(seed=0, n_ch=20, n_days=400):
    rng = np.random.default_rng(seed)
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n_days)]
    rows = []
    for d in days:
        for ch in range(n_ch):
            y = int(rng.random() < 0.2)
            rows.append({"ch": ch, "day": d, "x": y * 3.0 + rng.normal(), "y": y})
    df = pl.DataFrame(rows)
    return days, df.select(["ch", "day", "x"]), df.select(["ch", "day", "y"])


def test_oof_covers_only_test_windows():
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    prior = stacking.oof_predictions("A", feats, labels, splits,
                                     params={"n_estimators": 40})
    covered = set(prior["day"].to_list())
    expected = set()
    for s in splits:
        d = s.test_start
        while d <= s.test_end:
            expected.add(d)
            d += dt.timedelta(days=1)
    assert covered == expected, "приор существует ровно на тестовых окнах"


def test_oof_prior_carries_signal():
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    prior = stacking.oof_predictions("A", feats, labels, splits,
                                     params={"n_estimators": 60})
    joined = prior.join(labels, on=["ch", "day"], how="inner")
    pos = joined.filter(pl.col("y") == 1)["prior_A"].mean()
    neg = joined.filter(pl.col("y") == 0)["prior_A"].mean()
    assert pos > neg


def test_attach_prior_leaves_gaps_empty_not_zero():
    """Отсутствие приора — не нулевой риск, а неизвестность."""
    feats = pl.DataFrame({"ch": [1, 2], "day": [dt.date(2025, 1, 1)] * 2,
                          "x": [0.0, 1.0]})
    prior = pl.DataFrame({"ch": [1], "day": [dt.date(2025, 1, 1)],
                          "prior_A": [0.9]})
    out = stacking.attach_prior(feats, prior, on=["ch", "day"])
    assert out.filter(pl.col("ch") == 2)["prior_A"][0] is None


def test_attach_prior_is_noop_without_prior():
    feats = pl.DataFrame({"ch": [1], "day": [dt.date(2025, 1, 1)], "x": [0.0]})
    out = stacking.attach_prior(feats, pl.DataFrame(), on=["ch", "day"])
    assert out.columns == feats.columns


def test_recency_weight_halves_each_half_life():
    days = np.array(["2025-01-01", "2025-07-01", "2025-12-29"],
                    dtype="datetime64[D]")
    w = stacking.recency_weights(days, half_life_days=180.0)
    assert w[-1] == pytest.approx(1.0)
    assert w[1] == pytest.approx(0.5, abs=0.02), "полгода назад — половина веса"
    assert w[0] < w[1] < w[2]


def test_recency_weight_is_flat_for_single_day():
    days = np.array(["2025-01-01"] * 5, dtype="datetime64[D]")
    assert np.allclose(stacking.recency_weights(days), 1.0)
