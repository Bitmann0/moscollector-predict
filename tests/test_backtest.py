import numpy as np
import pandas as pd
import pytest
from ml.backtest import COUNTS, FEATURES, make_samples, select_threshold, split_samples


def fixture_frames():
    days = pd.date_range("2024-01-01", periods=20)
    daily = pd.DataFrame({"day": days, "channel": 1})
    for name in COUNTS:
        daily[name] = 0
    daily["events"] = 10
    daily.loc[10, "faults"] = 1
    coverage = pd.DataFrame({"day": days, "observed_hours": 24})
    return daily, coverage


def test_future_changes_labels_not_historical_features():
    daily, coverage = fixture_frames()
    original, _ = make_samples(daily, coverage)
    daily.loc[10, "faults"] = 0
    changed, _ = make_samples(daily, coverage)
    boundary = pd.Timestamp("2024-01-11")
    a = original.loc[original.as_of.eq(boundary)].iloc[0]
    b = changed.loc[changed.as_of.eq(boundary)].iloc[0]
    np.testing.assert_array_equal(a[FEATURES].values, b[FEATURES].values)
    assert a.target == 1 and b.target == 0


def test_gap_and_tail_are_not_negative_labels():
    daily, coverage = fixture_frames()
    coverage.loc[10, "observed_hours"] = 0
    samples, quality = make_samples(daily, coverage)
    assert not samples.as_of.eq("2024-01-11").any()
    assert not samples.as_of.eq("2024-01-21").any()
    assert quality["unknown_future"] > 0
    assert not samples.as_of.eq("2024-01-12").any()  # Current fault not a new prediction.


def test_split_purges_labels_crossing_boundary():
    points = pd.to_datetime(["2024-12-29", "2024-12-30", "2024-12-31",
                             "2025-02-01", "2025-02-02", "2025-11-01", "2025-11-02",
                             "2026-02-01", "2026-02-02"])
    samples = pd.DataFrame({"as_of": points, "label_end": points + pd.Timedelta(2, unit="D"),
                            "target": [0, 1, 0, 0, 1, 0, 1, 0, 1]})
    splits = split_samples(samples)
    assert len(splits["train"]) == 2
    assert splits["train"].label_end.max() <= splits["validation"].as_of.min()


def test_threshold_reports_unmet_precision():
    threshold, rule = select_threshold(np.array([0, 0, 1, 0] * 10), np.full(40, 0.25))
    assert rule == "fallback_f0.5_precision_constraint_unmet"
    assert threshold == pytest.approx(0.25)
