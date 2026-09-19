"""Walk-forward evaluation for temperature episode-onset prediction."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ml.backtest import metrics, select_threshold
from ml.temperature_hourly import HIGH_C, LOW_C, MIGRATION_END, MIGRATION_START, WINDOW_HOURS


@dataclass(frozen=True)
class Fold:
    name: str
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    threshold_start: str
    threshold_end: str
    test_start: str
    test_end: str


FOLDS = (
    Fold("test-2024h1", "2019-02-01", "2023-01-01", "2023-01-01", "2023-07-01",
         "2023-07-01", "2024-01-01", "2024-01-01", "2024-07-01"),
    Fold("test-2025h1", "2019-02-01", "2024-01-01", "2024-01-01", "2024-07-01",
         "2024-07-01", "2025-01-01", "2025-01-01", "2025-07-01"),
    Fold("test-2026h1", "2019-02-01", "2025-01-01", "2025-01-01", "2025-07-01",
         "2025-07-01", "2026-01-01", "2026-01-01", "2026-07-01"),
)


def channel_bucket(channel: int) -> int:
    digest = hashlib.sha256(str(int(channel)).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 5


def feature_columns() -> list[str]:
    columns: list[str] = []
    for hours in WINDOW_HOURS:
        columns.extend(
            [
                f"observed_seconds_{hours}h",
                f"events_{hours}h",
                f"numeric_records_{hours}h",
                f"value_min_{hours}h",
                f"value_max_{hours}h",
                f"value_mean_{hours}h",
                f"value_std_{hours}h",
                f"value_first_{hours}h",
                f"value_last_{hours}h",
                f"value_slope_per_hour_{hours}h",
                f"distinct_values_{hours}h",
                f"hours_since_value_{hours}h",
                f"numeric_fraction_{hours}h",
                f"value_change_{hours}h",
                f"value_range_{hours}h",
            ]
        )
    columns.extend(
        [
            "distance_to_low_24h",
            "distance_to_high_24h",
            "sampling_ratio_24_to_168h",
            "value_iqr_720h",
            "value_residual_iqr_720h",
            "weekday_sin",
            "weekday_cos",
            "year_sin",
            "year_cos",
        ]
    )
    return columns


def make_samples(frame: pd.DataFrame, clean_hours: int = 24) -> tuple[pd.DataFrame, dict]:
    if clean_hours not in (24, 72):
        raise ValueError("clean_hours must be 24 or 72")
    data = frame.copy()
    data["as_of"] = pd.to_datetime(data["as_of"])
    expected_future = (data["numeric_records_168h"] / 7).clip(lower=1)
    data["future_cadence_ratio"] = data["future_numeric_records"] / expected_future
    eligibility = (
        data["numeric_records_24h"].gt(0)
        & data["future_numeric_records"].gt(0)
        & data["future_cadence_ratio"].ge(0.25)
        & data[f"bad_seconds_{clean_hours}h"].eq(0)
    )
    migration = data.as_of.ge(MIGRATION_START) & data.as_of.lt(MIGRATION_END)
    selected = data.loc[eligibility & ~migration].copy()
    selected["target"] = selected.future_bad_seconds.gt(0).astype(int)
    selected["label_end"] = selected.as_of + pd.offsets.Day(1)
    selected["channel_bucket"] = selected.channel.map(channel_bucket)
    for hours in WINDOW_HOURS:
        selected[f"numeric_fraction_{hours}h"] = (
            selected[f"numeric_records_{hours}h"]
            / selected[f"events_{hours}h"].replace(0, np.nan)
        )
        selected[f"value_change_{hours}h"] = (
            selected[f"value_last_{hours}h"] - selected[f"value_first_{hours}h"]
        )
        selected[f"value_range_{hours}h"] = (
            selected[f"value_max_{hours}h"] - selected[f"value_min_{hours}h"]
        )
    selected["distance_to_low_24h"] = selected.value_min_24h - LOW_C
    selected["distance_to_high_24h"] = HIGH_C - selected.value_max_24h
    selected["sampling_ratio_24_to_168h"] = (
        selected.numeric_records_24h / expected_future.loc[selected.index]
    )
    selected["value_iqr_720h"] = selected.value_q75_720h - selected.value_q25_720h
    selected["value_residual_iqr_720h"] = (
        (selected.value_last_24h - selected.value_median_720h)
        / selected.value_iqr_720h.replace(0, np.nan)
    )
    selected["weekday_sin"] = np.sin(2 * np.pi * selected.as_of.dt.dayofweek / 7)
    selected["weekday_cos"] = np.cos(2 * np.pi * selected.as_of.dt.dayofweek / 7)
    selected["year_sin"] = np.sin(2 * np.pi * selected.as_of.dt.dayofyear / 365.25)
    selected["year_cos"] = np.cos(2 * np.pi * selected.as_of.dt.dayofyear / 365.25)
    quality = {
        "raw_rows": len(data),
        "eligible_rows": len(selected),
        "positives": int(selected.target.sum()),
        "excluded_unknown_or_low_future_cadence": int(
            (data.numeric_records_24h.gt(0) & ~(
                data.future_numeric_records.gt(0) & data.future_cadence_ratio.ge(0.25)
            )).sum()
        ),
        "clean_hours": clean_hours,
    }
    return selected, quality


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
            min_samples_leaf=30,
            learning_rate=0.05,
            l2_regularization=2.0,
            early_stopping=False,
            random_state=42,
        ),
    }


def _rule_score(data: pd.DataFrame) -> np.ndarray:
    distance = data[["distance_to_low_24h", "distance_to_high_24h"]].min(axis=1)
    trend = data.value_slope_per_hour_24h.abs().fillna(0)
    raw = (-distance + trend).to_numpy(dtype=float)
    return 1 / (1 + np.exp(-np.clip(raw, -30, 30)))


def ranking_metrics(data: pd.DataFrame, scores: np.ndarray) -> dict:
    ranked = data[["as_of", "target"]].copy()
    ranked["score"] = scores
    ranked = ranked.sort_values("score", ascending=False)
    positives = int(ranked.target.sum())
    half = max(1, int(np.ceil(positives * 0.5)))
    half_index = int(np.flatnonzero(ranked.target.cumsum().to_numpy() >= half)[0]) + 1
    result = {
        "precision_at_50pct_recall": half / half_index,
        "alerts_at_50pct_recall": half_index,
    }
    for budget in (1, 3):
        alerts = ranked.groupby("as_of", sort=False).head(budget)
        tp = int(alerts.target.sum())
        result[f"daily_budget_{budget}"] = {
            "alerts": len(alerts),
            "precision": tp / len(alerts) if len(alerts) else 0.0,
            "recall": tp / positives if positives else 0.0,
            "calendar_days": int(ranked.as_of.nunique()),
        }
    return result


def evaluate_fold(
    samples: pd.DataFrame, fold: Fold, features: list[str], unseen_channels: bool
) -> tuple[dict, object | None]:
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
        return {"fold": fold.name, "status": "insufficient_both_classes"}, None
    medians = train[features].median(numeric_only=True).fillna(0)
    matrices = {name: part[features].fillna(medians).fillna(0) for name, part in parts.items()}
    candidates = _models()
    validation_ap = {}
    for name, model in candidates.items():
        model.fit(matrices["train"], train.target)
        validation_ap[name] = float(
            average_precision_score(validation.target, model.predict_proba(matrices["validation"])[:, 1])
        )
    validation_ap["boundary_trend_rule"] = float(
        average_precision_score(validation.target, _rule_score(validation))
    )
    selected = max(validation_ap, key=validation_ap.get)
    model = candidates.get(selected)
    if model is None:
        threshold_scores = _rule_score(threshold_set)
        test_scores = _rule_score(test)
    else:
        threshold_scores = model.predict_proba(matrices["threshold"])[:, 1]
        test_scores = model.predict_proba(matrices["test"])[:, 1]
    threshold, threshold_rule = select_threshold(threshold_set.target, threshold_scores)
    test_metrics = metrics(test.target, test_scores, threshold)
    base_rate = float(test.target.mean())
    test_metrics["average_precision_lift"] = (
        test_metrics["average_precision"] / base_rate if base_rate else 0.0
    )
    importance = []
    if model is not None:
        measured = permutation_importance(
            model,
            matrices["validation"],
            validation.target,
            scoring="average_precision",
            n_repeats=1,
            random_state=42,
            n_jobs=1,
        )
        order = np.argsort(measured.importances_mean)[::-1][:12]
        importance = [
            {"feature": features[index], "ap_decrease": float(measured.importances_mean[index])}
            for index in order
        ]
    return {
        "fold": fold.name,
        "status": "ok",
        "boundaries": asdict(fold),
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
        "validation_permutation_importance": importance,
    }, {"model": model, "medians": medians, "features": features, "threshold": threshold}


def run(features_path: Path, output: Path, clean_hours: int = 24) -> dict:
    with duckdb.connect() as con:
        frame = con.execute("SELECT * FROM read_parquet(?)", [str(features_path)]).df()
    samples, quality = make_samples(frame, clean_hours)
    features = feature_columns()
    protocols = {}
    last_artifact = None
    for protocol, unseen in (("temporal_all_channels", False), ("unseen_channel_20pct", True)):
        results = []
        for fold in FOLDS:
            result, artifact = evaluate_fold(samples, fold, features, unseen)
            results.append(result)
            if artifact is not None:
                last_artifact = artifact
        valid = [item for item in results if item["status"] == "ok"]
        protocols[protocol] = {
            "folds": results,
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
        "experiment": f"temperature-episode-hourly-clean-{clean_hours}h",
        "target": (
            f"First numeric temperature outside [{LOW_C},{HIGH_C}] C in next 24h "
            f"after {clean_hours}h without an out-of-range value"
        ),
        "evaluation_status": "retrospective walk-forward; no claim of untouched holdout",
        "quality": quality,
        "features": features,
        "protocols": protocols,
        "versions": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
            "duckdb": duckdb.__version__,
        },
        "input_sha256": hashlib.sha256(features_path.read_bytes()).hexdigest(),
        "limitations": [
            "Out-of-range temperature is a proxy event, not a confirmed physical failure.",
            "The [3,40] C range remains an unconfirmed inferred threshold.",
            "The current channel catalog is applied to historical data without versioning.",
            "The 2026 period has been inspected previously and is reported only as retrospective evidence.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    if last_artifact is not None:
        joblib.dump(last_artifact, output / "latest_fold_model.joblib")
    (output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clean-hours", type=int, choices=[24, 72], default=24)
    args = parser.parse_args()
    report = run(args.features, args.output, args.clean_hours)
    print(json.dumps(report["protocols"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
