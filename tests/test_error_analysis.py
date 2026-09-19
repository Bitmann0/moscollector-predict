import json

import pandas as pd
from ml.error_analysis import analyze


def test_error_analysis_publishes_aggregates_without_channel_ids(tmp_path):
    predictions = tmp_path / "predictions.csv"
    report = tmp_path / "report.json"
    output = tmp_path / "errors.json"
    pd.DataFrame(
        {
            "channel": [10, 10, 20, 20],
            "target": [1, 0, 1, 0],
            "score": [0.9, 0.8, 0.2, 0.1],
            "target_ambiguous": [1, 0, 0, 0],
            "faults_7d": [1, 1, 0, 0],
            "ambiguous_seconds_7d": [1, 0, 0, 0],
            "gap_before_current_day": [1, 1, 2, 1],
        }
    ).to_csv(predictions, index=False)
    report.write_text(json.dumps({"threshold": 0.5, "experiment": "test"}))
    result = analyze(predictions, report, output)
    assert result["overall"]["confusion_matrix"] == {"tn": 1, "fp": 1, "fn": 1, "tp": 1}
    assert result["concentration"]["false_positives"]["channels"] == 1
    assert result["channel_summary"]["evaluated_channels"] == 2
    serialized = json.loads(output.read_text(encoding="utf-8"))
    assert "channel_ids" not in serialized["concentration"]["false_positives"]
    assert set(serialized["concentration"]["false_positives"]) == {
        "events",
        "channels",
        "top_1_channel_share",
        "top_5_channels_share",
        "top_10_channels_share",
    }
