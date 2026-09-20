"""Walk-forward model for temporary next-day telemetry outage onset."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ml.backtest import metrics, select_threshold
from ml.rolling_backtest import FOLDS, channel_bucket, ranking_metrics

NUMERIC_FEATURES = [
    "event_count",
    "alarm_count",
    "alarm_fraction",
    "days_since_previous",
    "observed_days_previous_30d",
    "events_previous_30d",
    "median_events_previous_30d",
    "alarms_previous_30d",
    "observed_days_previous_7d",
    "events_previous_7d",
    "activity_ratio_to_past",
    "events_vs_median_30d",
    "events_accel_7d_30d",
    "weekday_sin",
    "weekday_cos",
    "year_sin",
    "year_cos",
]


def make_samples(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    data = frame.copy()
    data["as_of"] = pd.to_datetime(data.pop("local_date")) + pd.offsets.Day(1)
    data["label_end"] = pd.to_datetime(data["label_end"])
    data["channel_bucket"] = data.channel_id.map(channel_bucket)
    data["events_vs_median_30d"] = (
        data.event_count / data.median_events_previous_30d.replace(0, np.nan)
    )
    data["events_accel_7d_30d"] = (
        (data.events_previous_7d / 7)
        / (data.events_previous_30d / 30).replace(0, np.nan)
    )
    data["weekday_sin"] = np.sin(2 * np.pi * data.as_of.dt.dayofweek / 7)
    data["weekday_cos"] = np.cos(2 * np.pi * data.as_of.dt.dayofweek / 7)
    data["year_sin"] = np.sin(2 * np.pi * data.as_of.dt.dayofyear / 365.25)
    data["year_cos"] = np.cos(2 * np.pi * data.as_of.dt.dayofyear / 365.25)
    sensor = pd.get_dummies(
        data.sensor_type.fillna("unknown"), prefix="sensor_type", dtype=float
    )
    data = pd.concat([data, sensor], axis=1)
    return data, NUMERIC_FEATURES + sensor.columns.tolist()


def _slice(data: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return data.loc[
        data.as_of.ge(start) & data.as_of.lt(end) & data.label_end.le(pd.Timestamp(end))
    ].copy()


def _models() -> dict:
    return {
        "logistic_regression": make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42),
        ),
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            max_iter=160,
            max_leaf_nodes=15,
            min_samples_leaf=50,
            learning_rate=0.05,
            l2_regularization=2.0,
            early_stopping=False,
            random_state=42,
        ),
    }


def _cadence_rule(data: pd.DataFrame) -> np.ndarray:
    raw = -data.activity_ratio_to_past.fillna(1).to_numpy(dtype=float)
    return 1 / (1 + np.exp(-np.clip(raw, -30, 30)))


def evaluate_fold(
    samples: pd.DataFrame, fold, features: list[str], unseen_channels: bool
) -> dict:
    train = _slice(samples, fold.train_start, fold.train_end)
    validation = _slice(samples, fold.validation_start, fold.validation_end)
    threshold_set = _slice(samples, fold.threshold_start, fold.threshold_end)
    test = _slice(samples, fold.test_start, fold.test_end)
    if unseen_channels:
        train = train.loc[train.channel_bucket.ne(0)]
        validation = validation.loc[validation.channel_bucket.eq(0)]
        threshold_set = threshold_set.loc[threshold_set.channel_bucket.eq(0)]
        test = test.loc[test.channel_bucket.eq(0)]
    parts = {"train": train, "validation": validation, "threshold": threshold_set, "test": test}
    if any(part.empty or part.target.nunique() != 2 for part in parts.values()):
        return {"fold": fold.name, "status": "insufficient_both_classes"}
    medians = train[features].median(numeric_only=True).fillna(0)
    matrices = {name: part[features].fillna(medians).fillna(0) for name, part in parts.items()}
    candidates = _models()
    validation_ap = {}
    for name, model in candidates.items():
        model.fit(matrices["train"], train.target)
        validation_ap[name] = float(
            average_precision_score(
                validation.target, model.predict_proba(matrices["validation"])[:, 1]
            )
        )
    validation_ap["current_cadence_rule"] = float(
        average_precision_score(validation.target, _cadence_rule(validation))
    )
    selected = max(validation_ap, key=validation_ap.get)
    model = candidates.get(selected)
    if model is None:
        threshold_scores = _cadence_rule(threshold_set)
        test_scores = _cadence_rule(test)
    else:
        threshold_scores = model.predict_proba(matrices["threshold"])[:, 1]
        test_scores = model.predict_proba(matrices["test"])[:, 1]
    threshold, threshold_rule = select_threshold(threshold_set.target, threshold_scores)
    test_metrics = metrics(test.target, test_scores, threshold)
    base_rate = float(test.target.mean())
    test_metrics["average_precision_lift"] = (
        test_metrics["average_precision"] / base_rate if base_rate else 0.0
    )
    return {
        "fold": fold.name,
        "status": "ok",
        "unseen_channels": unseen_channels,
        "selected_model": selected,
        "validation_average_precision": validation_ap,
        "threshold": threshold,
        "threshold_rule": threshold_rule,
        "splits": {
            name: {"samples": len(part), "positives": int(part.target.sum())}
            for name, part in parts.items()
        },
        "threshold_holdout": metrics(threshold_set.target, threshold_scores, threshold),
        "test": test_metrics,
        "test_ranking": ranking_metrics(test, test_scores),
    }


def run(samples_path: Path, output: Path) -> dict:
    manifest_path = samples_path.parent / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    with duckdb.connect() as con:
        frame = con.execute("SELECT * FROM read_parquet(?)", [str(samples_path)]).df()
    samples, features = make_samples(frame)
    protocols = {}
    for protocol, unseen in (("temporal_all_channels", False), ("unseen_channel_20pct", True)):
        folds = [evaluate_fold(samples, fold, features, unseen) for fold in FOLDS]
        valid = [item for item in folds if item["status"] == "ok"]
        protocols[protocol] = {
            "folds": folds,
            "mean_test": {
                key: float(np.mean([item["test"][key] for item in valid])) if valid else None
                for key in (
                    "precision",
                    "recall",
                    "average_precision",
                    "average_precision_lift",
                    "false_alerts_per_100_observed_channel_days",
                )
            },
        }
    report = {
        "experiment": (
            "telemetry-availability-active-"
            f"{manifest.get('min_active_days_previous_30d', 'unknown')}-of-30-"
            f"recovery-{manifest.get('recovery_days', 'unknown')}d"
        ),
        "target": manifest.get(
            "target", "Next calendar day has no channel telemetry; definition manifest missing"
        ),
        "positive_semantics": manifest.get(
            "positive_semantics", "telemetry availability incident"
        ),
        "target_manifest": manifest,
        "physical_failure_metrics_available": False,
        "evaluation_status": "retrospective walk-forward; no claim of untouched holdout",
        "features": features,
        "protocols": protocols,
        "input_sha256": hashlib.sha256(samples_path.read_bytes()).hexdigest(),
        "versions": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
            "duckdb": duckdb.__version__,
        },
        "limitations": [
            "The label measures telemetry availability, not physical failure or repair demand.",
            "Recovery within seven days censors long outages and decommissioned channels.",
            "The 2026 period has already been inspected and is retrospective evidence only.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.samples, args.output)
    print(json.dumps(report["protocols"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
