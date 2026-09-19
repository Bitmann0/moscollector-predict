import pandas as pd
from ml.rolling_backtest import channel_bucket, make_samples, ranking_metrics
from ml.temperature_hourly import WINDOW_HOURS


def _frame():
    rows = []
    for day, future, past_bad, future_count in (
        ("2024-01-01", 1, 0, 10),
        ("2024-01-02", 0, 1, 10),
        ("2024-01-03", 0, 0, 0),
    ):
        row = {
            "channel": 7,
            "as_of": day,
            "future_bad_seconds": future,
            "future_numeric_records": future_count,
            "value_q25_720h": 18,
            "value_median_720h": 20,
            "value_q75_720h": 22,
        }
        for hours in WINDOW_HOURS:
            row.update(
                {
                    f"observed_seconds_{hours}h": 10,
                    f"events_{hours}h": 10,
                    f"numeric_records_{hours}h": 10 if hours != 168 else 70,
                    f"bad_seconds_{hours}h": past_bad,
                    f"value_min_{hours}h": 18,
                    f"value_max_{hours}h": 22,
                    f"value_mean_{hours}h": 20,
                    f"value_std_{hours}h": 1,
                    f"value_first_{hours}h": 19,
                    f"value_last_{hours}h": 21,
                    f"value_slope_per_hour_{hours}h": 0.1,
                    f"distinct_values_{hours}h": 3,
                    f"hours_since_value_{hours}h": 1,
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)


def test_episode_samples_exclude_existing_bad_and_unknown_future():
    samples, quality = make_samples(_frame(), clean_hours=24)
    assert len(samples) == 1
    assert samples.iloc[0].target == 1
    assert quality["excluded_unknown_or_low_future_cadence"] == 1


def test_channel_bucket_is_deterministic():
    assert channel_bucket(123) == channel_bucket(123)
    assert 0 <= channel_bucket(123) < 5


def test_ranking_metrics_apply_daily_budget():
    frame = pd.DataFrame(
        {
            "as_of": pd.to_datetime(["2024-01-01"] * 2 + ["2024-01-02"] * 2),
            "target": [1, 0, 0, 1],
        }
    )
    result = ranking_metrics(frame, [0.9, 0.1, 0.8, 0.7])
    assert result["daily_budget_1"]["alerts"] == 2
    assert result["daily_budget_1"]["precision"] == 0.5
    assert result["precision_at_50pct_recall"] == 1.0
