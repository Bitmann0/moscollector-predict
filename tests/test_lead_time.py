import numpy as np
import pandas as pd
from ml.backtest import FEATURES, OBSERVATION_FEATURES, make_samples


def frames():
    days = pd.date_range("2024-01-01", periods=20)
    daily = pd.DataFrame(
        {
            "day": days,
            "channel": 10,
            "events": 10,
            "alarms": 0,
            "faults": 0,
            "undefined": 0,
            "power_loss": 0,
            "running": 0,
            "ambiguous_seconds": 0,
        }
    )
    daily.loc[10, "faults"] = 1
    return daily, pd.DataFrame({"day": days, "observed_hours": 24})


def test_24h_minimum_lead_does_not_count_near_term_signals_as_success():
    daily, coverage = frames()
    samples, _ = make_samples(daily, coverage, lead_days=1)
    early = samples.loc[samples.as_of.eq("2024-01-10")].iloc[0]
    late = samples.loc[samples.as_of.eq("2024-01-11")].iloc[0]
    assert early.target == 1 and late.target == 0
    assert late.near_term_signal
    assert (samples.label_start - samples.as_of).eq(pd.Timedelta(days=1)).all()
    assert (samples.label_end - samples.as_of).eq(pd.Timedelta(days=2)).all()


def test_two_day_labels_require_observation_of_both_future_days():
    daily, coverage = frames()
    daily.loc[10, "events"] = 0
    samples, _ = make_samples(daily, coverage, lead_days=1)
    assert not samples.as_of.eq("2024-01-10").any()
    assert not samples.as_of.eq("2024-01-11").any()
    assert not samples.as_of.eq("2024-01-20").any()


def test_observation_features_use_only_the_past_and_retain_gaps():
    daily, coverage = frames()
    daily = daily.drop(index=[6, 7])
    samples, _ = make_samples(daily, coverage, lead_days=1)
    before = samples.loc[samples.as_of.eq("2024-01-10")].iloc[0]
    assert before.observed_days_7d == 5
    assert before.gap_before_current_day == 3
    daily.loc[daily.day.eq("2024-01-11"), "faults"] = 0
    changed, _ = make_samples(daily, coverage, lead_days=1)
    after = changed.loc[changed.as_of.eq("2024-01-10")].iloc[0]
    np.testing.assert_array_equal(
        before[FEATURES + OBSERVATION_FEATURES], after[FEATURES + OBSERVATION_FEATURES]
    )
    assert before.target == 1 and after.target == 0
