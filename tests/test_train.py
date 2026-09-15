import datetime as dt

import numpy as np
import polars as pl

from mkl import cv, train


def _dataset(seed=0, signal=3.0):
    rng = np.random.default_rng(seed)
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(400)]
    rows = []
    for d in days:
        for ch in range(20):
            y = int(rng.random() < 0.2)
            rows.append({"ch": ch, "day": d,
                         "x": y * signal + rng.normal(),
                         "noise": rng.normal(),
                         "y": y})
    df = pl.DataFrame(rows)
    return days, df.select(["ch", "day", "x", "noise"]), df.select(["ch", "day", "y"])


def test_run_learns_a_separable_signal():
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 60}, budget_per_day=5)
    assert out["mean"]["pr_auc"] > 0.5
    assert len(out["folds"]) == 2


def test_run_finds_no_signal_in_pure_noise():
    days, feats, labels = _dataset(signal=0.0)
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 60}, budget_per_day=5)
    assert out["mean"]["pr_auc"] < 0.35, "на чистом шуме PR-AUC не должен уходить далеко от базовой ставки 0.2"


def test_feature_columns_excludes_keys_and_target():
    _, feats, labels = _dataset()
    data = feats.join(labels, on=["ch", "day"], how="inner")
    cols = train.feature_columns(data)
    assert set(cols) == {"x", "noise"}


def test_importance_ranks_the_informative_feature_first():
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 60}, budget_per_day=5)
    top = train.importance(out["model"], out["feature_names"], top=1)
    assert top["feature"][0] == "x"
