import pandas as pd
from ml.availability_backtest import make_samples


def test_availability_features_do_not_include_channel_id():
    frame = pd.DataFrame(
        {
            "channel_id": [1],
            "local_date": ["2024-01-01"],
            "label_end": ["2024-01-03"],
            "target": [1],
            "event_count": [10],
            "alarm_count": [0],
            "alarm_fraction": [0],
            "days_since_previous": [1],
            "observed_days_previous_30d": [30],
            "events_previous_30d": [300],
            "median_events_previous_30d": [10],
            "alarms_previous_30d": [0],
            "observed_days_previous_7d": [7],
            "events_previous_7d": [70],
            "activity_ratio_to_past": [1],
            "sensor_type": ["temperature"],
        }
    )
    samples, features = make_samples(frame)
    assert "channel_id" not in features
    assert "sensor_type_temperature" in features
    assert samples.iloc[0].as_of == pd.Timestamp("2024-01-02")
