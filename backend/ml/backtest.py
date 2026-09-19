"""Temporal evaluation of recorded pump fault signals on censored daily windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, confusion_matrix
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

COUNTS = ["events", "alarms", "faults", "undefined", "power_loss", "running", "ambiguous_seconds"]
FEATURES = [f"{name}_{window}d" for name in COUNTS for window in (1, 7)] + [
    "days_since_fault",
    "weekday_sin",
    "weekday_cos",
]
OBSERVATION_FEATURES = ["observed_days_7d", "gap_before_current_day"]


def make_samples(
    daily: pd.DataFrame, coverage: pd.DataFrame, lead_days: int = 0
) -> tuple[pd.DataFrame, dict]:
    if lead_days not in (0, 1):
        raise ValueError("Supported minimum leads: zero or one day")
    daily = daily.copy()
    coverage = coverage.copy()
    daily["day"] = pd.to_datetime(daily["day"])
    coverage["day"] = pd.to_datetime(coverage["day"])
    if daily.duplicated(["channel", "day"]).any() or coverage.duplicated("day").any():
        raise ValueError("Duplicate daily keys")
    days = pd.date_range(coverage.day.min(), coverage.day.max(), freq="D")
    covered = coverage.set_index("day").reindex(days).observed_hours.eq(24)
    history_covered = covered.rolling(7, min_periods=7).sum().eq(7)
    future_covered = covered.shift(-1, fill_value=False)
    for offset in range(2, lead_days + 2):
        future_covered &= covered.shift(-offset, fill_value=False)
    parts = []
    counters = {"channel_days": 0, "eligible_at_prediction": 0, "unknown_future": 0}
    for channel, source in daily.groupby("channel"):
        frame = source.set_index("day").reindex(days)
        frame[COUNTS] = frame[COUNTS].fillna(0)
        features = pd.DataFrame(index=days)
        for name in COUNTS:
            for window in (1, 7):
                features[f"{name}_{window}d"] = frame[name].rolling(window).sum()
        last_fault = pd.Series(days, index=days).where(frame.faults.gt(0)).ffill()
        features["days_since_fault"] = (pd.Series(days, index=days) - last_fault).dt.days.fillna(
            999
        )
        features["weekday_sin"] = np.sin(
            2 * np.pi * (days + pd.Timedelta(1, unit="D")).dayofweek / 7
        )
        features["weekday_cos"] = np.cos(
            2 * np.pi * (days + pd.Timedelta(1, unit="D")).dayofweek / 7
        )
        features["channel"] = int(channel)
        features["as_of"] = days + pd.Timedelta(1, unit="D")
        features["label_start"] = days + pd.Timedelta(lead_days + 1, unit="D")
        features["label_end"] = days + pd.Timedelta(lead_days + 2, unit="D")
        observed = frame.events.gt(0)
        features["observed_days_7d"] = observed.rolling(7).sum()
        previous_observation = pd.Series(days, index=days).where(observed).shift(1).ffill()
        features["gap_before_current_day"] = (
            pd.Series(days, index=days) - previous_observation
        ).dt.days.fillna(999)
        # Only predict recurrence after a full day without a fault record.
        # This does not prove that the physical device had recovered.
        eligible = history_covered & frame.events.gt(0) & frame.faults.eq(0)
        # Silence is not a healthy target: require observable future telemetry.
        known = future_covered & frame.events.shift(-1).gt(0)
        earlier_signal = pd.Series(False, index=days)
        for offset in range(1, lead_days + 1):
            earlier_signal |= frame.faults.shift(-offset).gt(0)
            known &= frame.events.shift(-offset - 1).gt(0)
        # With a 24h minimum lead, a signal in the first day is too early to be a positive.
        # It remains in evaluation, not silently removed using future information.
        features["near_term_signal"] = earlier_signal
        features["target"] = (frame.faults.shift(-lead_days - 1).gt(0) & ~earlier_signal).astype(
            int
        )
        features["target_ambiguous"] = frame.ambiguous_seconds.shift(-lead_days - 1).gt(0)
        counters["channel_days"] += len(frame)
        counters["eligible_at_prediction"] += int(eligible.sum())
        counters["unknown_future"] += int((eligible & ~known).sum())
        parts.append(features.loc[eligible & known])
    if not parts:
        raise ValueError("No pump channels available")
    result = pd.concat(parts, ignore_index=True).sort_values(["as_of", "channel"])
    counters["evaluated_samples"] = len(result)
    counters["positive_samples"] = int(result.target.sum())
    return result, counters


def split_samples(samples: pd.DataFrame) -> dict[str, pd.DataFrame]:
    boundaries = [
        ("train", "2024-01-01", "2025-01-01"),
        ("validation", "2025-01-01", "2025-10-01"),
        ("threshold", "2025-10-01", "2026-01-01"),
        ("test", "2026-01-01", "2026-07-01"),
    ]
    splits = {}
    for name, start, end in boundaries:
        subset = samples.loc[
            (samples.as_of >= start) & (samples.as_of < end) & (samples.label_end <= end)
        ].copy()
        if subset.target.nunique() != 2:
            raise ValueError(f"{name}: both target classes required; got {len(subset)} samples")
        splits[name] = subset
    return splits


def metrics(target, scores, threshold: float) -> dict:
    y = np.asarray(target, dtype=int)
    scores = np.asarray(scores)
    predicted = scores >= threshold
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel().tolist()
    return {
        "samples": len(y),
        "positives": int(y.sum()),
        "alerts": int(predicted.sum()),
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "average_precision": float(average_precision_score(y, scores)) if y.sum() else 0.0,
        "brier_score": float(brier_score_loss(y, scores)),
        "false_alerts_per_100_observed_channel_days": 100 * fp / len(y),
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def select_threshold(target, scores, minimum_precision=0.7) -> tuple[float, str]:
    # Threshold selection uses only the dedicated late-2025 holdout. Test labels
    # never participate. Minimum support avoids a one-alert precision of 100%.
    candidates = np.unique(np.r_[np.quantile(scores, np.linspace(0, 1, 201)), 1.0])
    evaluations = [(float(t), metrics(target, scores, float(t))) for t in candidates]
    acceptable = [
        (t, m) for t, m in evaluations if m["precision"] > minimum_precision and m["alerts"] >= 10
    ]
    if acceptable:
        return max(acceptable, key=lambda item: (item[1]["recall"], item[0]))[
            0
        ], "precision_constraint"

    def f_half(item):
        p, r = item[1]["precision"], item[1]["recall"]
        return 1.25 * p * r / (0.25 * p + r) if p + r else 0.0

    return max(evaluations, key=f_half)[0], "fallback_f0.5_precision_constraint_unmet"


def run(
    data_dir: Path,
    output: Path,
    lead_days: int = 0,
    observation_features: bool = False,
    target_description: str | None = None,
    experiment_name: str | None = None,
) -> dict:
    started = time.perf_counter()
    daily_path, coverage_path = data_dir / "pump_daily.csv", data_dir / "coverage.csv"
    feature_names = FEATURES + (OBSERVATION_FEATURES if observation_features else [])
    samples, quality = make_samples(pd.read_csv(daily_path), pd.read_csv(coverage_path), lead_days)
    splits = split_samples(samples)
    train, validation = splits["train"], splits["validation"]
    models = {
        "logistic_regression": make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42),
        ),
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            max_iter=120,
            max_leaf_nodes=15,
            min_samples_leaf=30,
            learning_rate=0.06,
            l2_regularization=2.0,
            early_stopping=False,
            random_state=42,
        ),
    }
    comparisons = {}
    for name, model in models.items():
        model.fit(train[feature_names], train.target)
        scores = model.predict_proba(validation[feature_names])[:, 1]
        comparisons[name] = float(average_precision_score(validation.target, scores))
    selected = max(comparisons, key=comparisons.get)
    model = models[selected]
    holdout, test = splits["threshold"], splits["test"]
    threshold, threshold_rule = select_threshold(
        holdout.target, model.predict_proba(holdout[feature_names])[:, 1]
    )
    inference_start = time.perf_counter()
    test_scores = model.predict_proba(test[feature_names])[:, 1]
    inference_seconds = time.perf_counter() - inference_start
    baseline_rate = float(train.target.mean())
    recent_rule = test.faults_7d.gt(0).astype(float)
    report = {
        "experiment": experiment_name
        or f"pump-signal-lead-{lead_days}d-observation-{observation_features}",
        "target": target_description
        or (
            "At least one Неисправен record in next calendar day after a day without it"
            if lead_days == 0
            else "No recorded fault in [t,t+24h), at least one in [t+24h,t+48h), after a clean observed day"
        ),
        "minimum_lead_hours": 24 * lead_days,
        "evaluation_status": "exploratory reuse of previously inspected calendar holdout",
        "horizon_hours": 24 * (lead_days + 1),
        "target_window_hours": 24,
        "physical_failure_metrics_available": False,
        "timezone_assumption": "Europe/Moscow",
        "quality": quality,
        "selected_model": selected,
        "model_selection_validation_ap": comparisons,
        "threshold": threshold,
        "threshold_rule": threshold_rule,
        "features": feature_names,
        "train_base_rate": baseline_rate,
        "splits": {
            name: {
                "samples": len(part),
                "positives": int(part.target.sum()),
                "as_of_min": str(part.as_of.min()),
                "as_of_max": str(part.as_of.max()),
                "label_end_max": str(part.label_end.max()),
            }
            for name, part in splits.items()
        },
        "threshold_holdout": metrics(
            holdout.target, model.predict_proba(holdout[feature_names])[:, 1], threshold
        ),
        "test": metrics(test.target, test_scores, threshold),
        "constant_baseline_test": metrics(
            test.target, np.full(len(test), baseline_rate), threshold
        ),
        "recent_fault_rule_test": metrics(test.target, recent_rule, 0.5),
        "test_by_month": {},
        "test_inference_seconds": inference_seconds,
        "experiment_seconds": time.perf_counter() - started,
        "versions": {
            "python": platform.python_version(),
            "sklearn": sklearn.__version__,
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "input_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (daily_path, coverage_path)
        },
        "limitations": [
            "Weak target is message presence, not confirmed physical failure or episode onset.",
            "Same-second states remain unordered; Неисправен may coexist with Норма.",
            "24-hour global coverage is a heuristic; source and channel completeness unverified.",
            "Evaluation requires future channel telemetry; excludes silence, potentially informative.",
            "Current catalog is applied to history without equipment versioning.",
            "Scores are uncalibrated; lead constraints concern records, not physical failures.",
            "A single held-out period; exploratory results need independent confirmation.",
        ],
    }
    indexed = test.assign(score=test_scores)
    for month, part in indexed.groupby(indexed.as_of.dt.to_period("M")):
        report["test_by_month"][str(month)] = metrics(part.target, part.score, threshold)
    report["user_precision_recall_targets_met"] = (
        report["test"]["precision"] > 0.7 and report["test"]["recall"] > 0.5
    )
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "features": feature_names,
            "threshold": threshold,
            "target": report["target"],
        },
        output / "model.joblib",
    )
    indexed.to_csv(output / "test_predictions.csv", index=False)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "selected_model": selected,
                "threshold_rule": threshold_rule,
                "test": report["test"],
                "quality": quality,
            },
            indent=2,
        ),
        flush=True,
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/processed/pumps"))
    parser.add_argument("--output", type=Path, default=Path("data/models/pump-signal-v1"))
    parser.add_argument("--lead-days", type=int, choices=[0, 1], default=0)
    parser.add_argument("--observation-features", action="store_true")
    parser.add_argument("--target-description")
    parser.add_argument("--experiment-name")
    args = parser.parse_args()
    run(
        args.data_dir,
        args.output,
        args.lead_days,
        args.observation_features,
        args.target_description,
        args.experiment_name,
    )


if __name__ == "__main__":
    main()
