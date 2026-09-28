"""Walk-forward бэктест температурных эпизодов: признаки, модели, порог, отчёт.

Перенос rolling_backtest.py из PR #8 (135a6a3) поверх событий mkl. Метка и
признаки — в mkl.temperature_episode. Подкоманды:

  features                    почасовые признаки в data/features и манифест
                              reports/temperature_episode_sources.json
  backtest --clean-hours 24   три фолда, три модели, отчёт
  backtest --clean-hours 72   reports/temperature_episode_{24,72}h.json
  audit                       reports/temperature_episode_target_audit.json

Все три требуют полного журнала: в бандле из событий есть только 2026 год
(build_bundle.py, REQUIRED), а фолды начинаются с 2019-го.

Фолды — как в PR: train, validation, threshold и test идут встык, без зазора.
Протокол mkl держит EMBARGO_DAYS = 31 (config.py). Утечки метки нет и без
зазора — _slice требует label_end <= конец периода, — но 720-часовые признаки
первых суток нового периода видят те же записи, что метки конца предыдущего.
Поэтому зазор считается отдельным протоколом, а не заменяет исходные фолды:
иначе числа PR не с чем было бы сверить.

Тест 2026H1 совпадает с отложенным периодом mkl. Каждый прогон backtest пишет
запись step=FINAL в experiments/log.jsonl, и holdout_uses.py её учитывает.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, confusion_matrix
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from mkl import db, experiments
from mkl.config import EMBARGO_DAYS, HOLDOUT_END, HOLDOUT_START, PATHS
from mkl.temperature_episode import (
    FEATURES_FILE, HIGH_C, LOW_C, build_features, feature_columns, make_samples,
    sha256_file, target_audit, write_json,
)

HEAD = "temperature_episode"
CATALOG = PATHS.materials / "справочник_каналов_датчиков.csv"


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


def with_embargo(fold: Fold, days: int = EMBARGO_DAYS) -> Fold:
    """Тот же фолд с зазором в days суток перед validation, threshold и test.

    Сдвигаются концы ранних периодов, а не начала поздних: тестовое полугодие
    остаётся тем же, и результат сравним с исходным фолдом строка в строку.
    """
    def cut(end: str) -> str:
        return (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()

    return replace(fold, name=f"{fold.name}-embargo{days}",
                   train_end=cut(fold.train_end),
                   validation_end=cut(fold.validation_end),
                   threshold_end=cut(fold.threshold_end))


# binary_metrics и select_threshold скопированы из backend/ml/backtest.py ветки
# PR (там — metrics). Переименование — чтобы не путать с mkl.metrics. Логика
# дословная: сетка из 201 квантиля, не меньше 10 алертов, запасное F0.5. Замена
# на mkl.metrics.target_operating_point не воспроизвела бы числа PR — все 12
# прогонов фолдов в нём шли по запасному правилу.
def binary_metrics(target, scores, threshold: float) -> dict:
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
    evaluations = [(float(t), binary_metrics(target, scores, float(t))) for t in candidates]
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


def _slice(data: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    # label_end <= end: метка строки целиком лежит внутри периода, иначе
    # последние сутки train подглядывали бы в первые сутки следующего периода.
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
    """Правило без обучения: близость к границе плюс модуль суточного тренда."""
    distance = data[["distance_to_low_24h", "distance_to_high_24h"]].min(axis=1)
    trend = data.value_slope_per_hour_24h.abs().fillna(0)
    raw = (-distance + trend).to_numpy(dtype=float)
    return 1 / (1 + np.exp(-np.clip(raw, -30, 30)))


def ranking_metrics(data: pd.DataFrame, scores: np.ndarray) -> dict:
    """Точность при 50% полноты и при дневном лимите 1 и 3 алерта."""
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
            average_precision_score(validation.target,
                                    model.predict_proba(matrices["validation"])[:, 1])
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
    test_metrics = binary_metrics(test.target, test_scores, threshold)
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
        "threshold_holdout": binary_metrics(threshold_set.target, threshold_scores, threshold),
        "test": test_metrics,
        "test_ranking": ranking_metrics(test, test_scores),
        "validation_permutation_importance": importance,
    }, {"model": model, "medians": medians, "features": features, "threshold": threshold}


MEAN_KEYS = ("precision", "recall", "average_precision", "average_precision_lift",
             "false_alerts_per_100_observed_channel_days")


def _touches_holdout(fold: Fold) -> bool:
    return (dt.date.fromisoformat(fold.test_start) <= HOLDOUT_END
            and dt.date.fromisoformat(fold.test_end) > HOLDOUT_START)


def _log_holdout_views(protocol: str, clean_hours: int, folds: list[Fold],
                       results: list[dict]) -> None:
    """Запись step=FINAL на каждый фолд, чей тест попал в отложенный период."""
    for fold, result in zip(folds, results):
        if result["status"] != "ok" or not _touches_holdout(fold):
            continue
        test = result["test"]
        experiments.log({
            "head": HEAD, "step": "FINAL",
            "note": f"ретроспективно {fold.name}, {protocol}, чистое окно {clean_hours} ч",
            "protocol": protocol, "clean_hours": clean_hours,
            "precision": test["precision"], "recall": test["recall"],
            "pr_auc": test["average_precision"], "n": test["samples"],
            "n_pos": test["positives"], "threshold_rule": result["threshold_rule"],
        })


def run(features_path: Path, report_path: Path, clean_hours: int = 24,
        model_path: Path | None = None) -> dict:
    with duckdb.connect() as con:
        frame = con.execute("SELECT * FROM read_parquet(?)", [str(features_path)]).df()
    samples, quality = make_samples(frame, clean_hours)
    features = feature_columns()
    protocols = {}
    views = []
    last_artifact = None
    plans = (
        ("temporal_all_channels", False, list(FOLDS)),
        ("unseen_channel_20pct", True, list(FOLDS)),
        (f"temporal_all_channels_embargo{EMBARGO_DAYS}", False,
         [with_embargo(f) for f in FOLDS]),
    )
    for protocol, unseen, folds in plans:
        results = []
        for fold in folds:
            result, artifact = evaluate_fold(samples, fold, features, unseen)
            results.append(result)
            if artifact is not None and protocol == "temporal_all_channels":
                last_artifact = artifact
        valid = [item for item in results if item["status"] == "ok"]
        protocols[protocol] = {
            "folds": results,
            "mean_test": {
                key: float(np.mean([item["test"][key] for item in valid])) if valid else None
                for key in MEAN_KEYS
            },
        }
        views.append((protocol, folds, results))
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
        "embargo_days": EMBARGO_DAYS,
        "versions": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
            "duckdb": duckdb.__version__,
        },
        "input_sha256": sha256_file(Path(features_path)),
        "limitations": [
            "Out-of-range temperature is a proxy event, not a confirmed physical failure.",
            "The [3,40] C range remains an unconfirmed inferred threshold.",
            "The current channel catalog is applied to historical data without versioning.",
            ("The 2026 period has been inspected previously and is reported only as "
             "retrospective evidence."),
            ("Folds temporal_all_channels and unseen_channel_20pct have no gap between "
             "train, validation, threshold and test."),
        ],
    }
    if model_path is not None and last_artifact is not None:
        import joblib

        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(last_artifact, model_path)
    write_json(report_path, report)
    # Журнал пишется разом после отчёта, а не после каждого протокола.
    # holdout_uses.py склеивает записи FINAL в один просмотр по минуте и коду:
    # если протокол считается дольше минуты, запись после каждого протокола
    # засчитала бы один прогон тремя просмотрами.
    for protocol, folds, results in views:
        _log_holdout_views(protocol, clean_hours, folds, results)
    return report


def _connect(events: str | None):
    con = db.connect("4GB", 4)
    if events:
        escaped = events.replace("'", "''")
        con.execute(f"CREATE OR REPLACE VIEW ev AS SELECT * FROM read_parquet('{escaped}')")
    else:
        db.attach_events(con)
    return con


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    default_features = PATHS.features / FEATURES_FILE

    p = sub.add_parser("features")
    p.add_argument("--events", help="шаблон parquet событий вместо data/interim")
    p.add_argument("--out", type=Path, default=default_features)
    p.add_argument("--raw-dir", type=Path, default=PATHS.raw)
    p.add_argument("--catalog", type=Path, default=CATALOG)
    p.add_argument("--manifest", type=Path,
                   default=PATHS.reports / "temperature_episode_sources.json")

    p = sub.add_parser("backtest")
    p.add_argument("--clean-hours", type=int, choices=[24, 72], default=24)
    p.add_argument("--features", type=Path, default=default_features)
    p.add_argument("--report", type=Path)
    p.add_argument("--save-model", action="store_true",
                   help="модель последнего фолда в models/, в git она не попадает")

    p = sub.add_parser("audit")
    p.add_argument("--events", help="шаблон parquet событий вместо data/interim")
    p.add_argument("--features", type=Path, default=default_features)
    p.add_argument("--out", type=Path,
                   default=PATHS.reports / "temperature_episode_target_audit.json")

    args = parser.parse_args(argv)
    if args.command == "features":
        con = _connect(args.events)
        manifest = build_features(con, args.out, raw_dir=args.raw_dir,
                                  catalog_path=args.catalog)
        con.close()
        write_json(args.manifest, manifest)
        print(json.dumps({k: v for k, v in manifest.items() if k != "sources"},
                         ensure_ascii=False, indent=2))
    elif args.command == "backtest":
        report_path = args.report or (
            PATHS.reports / f"temperature_episode_{args.clean_hours}h.json")
        model_path = (PATHS.models / "temperature_episode_latest_fold.joblib"
                      if args.save_model else None)
        report = run(args.features, report_path, args.clean_hours, model_path)
        print(json.dumps({k: v["mean_test"] for k, v in report["protocols"].items()},
                         ensure_ascii=False, indent=2))
    else:
        with duckdb.connect() as fcon:
            frame = fcon.execute("SELECT * FROM read_parquet(?)",
                                 [str(args.features)]).df()
        samples, _ = make_samples(frame, 72)
        con = _connect(args.events)
        audit = target_audit(con, samples)
        con.close()
        write_json(args.out, audit)
        print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
