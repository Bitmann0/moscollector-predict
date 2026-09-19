"""Aggregate model errors without publishing channel identifiers or row-level predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ml.backtest import metrics


def _safe_metrics(frame: pd.DataFrame, threshold: float) -> dict:
    if frame.empty:
        return {"samples": 0}
    return metrics(frame.target, frame.score, threshold)


def _concentration(frame: pd.DataFrame, mask: pd.Series) -> dict:
    counts = frame.loc[mask].groupby("channel").size().sort_values(ascending=False)
    total = int(counts.sum())
    return {
        "events": total,
        "channels": len(counts),
        "top_1_channel_share": float(counts.head(1).sum() / total) if total else None,
        "top_5_channels_share": float(counts.head(5).sum() / total) if total else None,
        "top_10_channels_share": float(counts.head(10).sum() / total) if total else None,
    }


def analyze(predictions: Path, model_report: Path, output: Path) -> dict:
    frame = pd.read_csv(predictions)
    source_report = json.loads(model_report.read_text(encoding="utf-8"))
    threshold = float(source_report["threshold"])
    predicted = frame.score.ge(threshold)
    target = frame.target.astype(bool)
    groups = {
        "target_day_contains_multi_state_second": frame.target_ambiguous.astype(bool),
        "target_day_has_no_multi_state_second": ~frame.target_ambiguous.astype(bool),
        "fault_recorded_in_previous_7_days": frame.faults_7d.gt(0),
        "no_fault_recorded_in_previous_7_days": frame.faults_7d.eq(0),
        "ambiguous_state_in_previous_7_days": frame.ambiguous_seconds_7d.gt(0),
        "no_ambiguous_state_in_previous_7_days": frame.ambiguous_seconds_7d.eq(0),
        "observation_gap_before_prediction": frame.gap_before_current_day.gt(1),
        "continuous_daily_observation_before_prediction": frame.gap_before_current_day.eq(1),
    }
    subgroup_metrics = {
        name: _safe_metrics(frame.loc[mask], threshold) for name, mask in groups.items()
    }
    bins = min(10, int(frame.score.nunique()))
    frame["score_bin"] = pd.qcut(frame.score, q=bins, duplicates="drop")
    calibration = []
    for rank, (_, part) in enumerate(frame.groupby("score_bin", observed=True), start=1):
        calibration.append(
            {
                "bin": rank,
                "samples": len(part),
                "score_min": float(part.score.min()),
                "score_max": float(part.score.max()),
                "mean_score": float(part.score.mean()),
                "observed_rate": float(part.target.mean()),
            }
        )
    report = {
        "experiment": source_report["experiment"],
        "threshold": threshold,
        "overall": _safe_metrics(frame, threshold),
        "subgroups": subgroup_metrics,
        "concentration": {
            "positive_targets": _concentration(frame, target),
            "alerts": _concentration(frame, predicted),
            "true_positives": _concentration(frame, predicted & target),
            "false_positives": _concentration(frame, predicted & ~target),
            "false_negatives": _concentration(frame, ~predicted & target),
        },
        "score_deciles": calibration,
        "channel_summary": {
            "evaluated_channels": int(frame.channel.nunique()),
            "channels_with_positive_target": int(frame.loc[target, "channel"].nunique()),
            "channels_with_alert": int(frame.loc[predicted, "channel"].nunique()),
            "median_samples_per_channel": float(frame.groupby("channel").size().median()),
        },
        "privacy": "Only aggregate counts are published; channel identifiers and predictions are excluded.",
        "limitations": [
            "Subgroups are descriptive on the already inspected 2026 holdout.",
            "A target is a recorded signal, not a confirmed physical failure.",
            (
                "Target-day ambiguity means at least one second that day has multiple states "
                "without reliable order; it need not be the target timestamp."
            ),
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.predictions, args.report, args.output)
    print(json.dumps(report["channel_summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
