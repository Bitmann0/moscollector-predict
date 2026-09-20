import duckdb
import pandas as pd
from ml.availability_prepare import prepare


def test_strict_availability_target_requires_recovery(tmp_path):
    path = tmp_path / "daily.parquet"
    rows = []
    for channel, days in (
        (1, [1, 3, 4]),
        (2, [1, 9]),
        (3, [1, 2, 3, 4]),
        (4, list(range(1, 10))),
    ):
        for day in days:
            rows.append(
                {
                    "channel_id": channel,
                    "local_date": pd.Timestamp(2024, 1, day),
                    "event_count": 10,
                    "alarm_count": 0,
                    "alarm_fraction": 0,
                    "previous_observed_date": pd.NaT,
                    "days_since_previous": 1,
                    "observed_days_previous_30d": 25,
                    "events_previous_30d": 300,
                    "median_events_previous_30d": 10,
                    "alarms_previous_30d": 0,
                    "observed_days_previous_7d": 7,
                    "events_previous_7d": 70,
                    "sensor_type": "test",
                    "feature_available_at": pd.Timestamp(2024, 1, day + 1),
                    "activity_ratio_to_past": 1,
                    "baseline_available": True,
                    "source_sha256": "sha",
                }
            )
    with duckdb.connect() as con:
        con.register("rows", pd.DataFrame(rows))
        con.execute("COPY rows TO ? (FORMAT PARQUET)", [str(path)])
    manifest = prepare(path, tmp_path / "out", recovery_days=7)
    with duckdb.connect() as con:
        samples = con.execute(
            "SELECT channel_id,local_date,target FROM read_parquet(?) ORDER BY 1,2",
            [str(tmp_path / "out" / "availability_samples.parquet")],
        ).df()
    assert manifest["positives"] == 1
    assert samples.loc[samples.target.eq(1), "channel_id"].tolist() == [1]
    assert 2 not in samples.channel_id.tolist()
