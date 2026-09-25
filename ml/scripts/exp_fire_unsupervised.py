"""Can an unlabeled normal-pattern model rank tomorrow's fire alarms?

Fit only on quiet 2023-2024 windows. Evaluate three 2025 time blocks on the
same quiet-week candidates as the supervised onset experiment. This tests
anomaly detection, not detection of confirmed fires.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler

from mkl import cv, db, labels, metrics, serve, store
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, TRAIN_END, END = (dt.date(2023, 1, 1), dt.date(2024, 12, 31),
                         dt.date(2025, 12, 31))
SIGNALS = ("n_events", "n_alarms", "n_bad", "temp_max", "temp_range")
COUNT_SIGNALS = {"n_events", "n_alarms", "n_bad"}


def windows(frame: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    frame = frame.sort(["obj", "seg", "day"])
    cols = []
    exprs = []
    for lag in range(7):
        for signal in SIGNALS:
            name = f"seq_{signal}_{lag}"
            value = pl.col(signal).cast(pl.Float64)
            if signal in COUNT_SIGNALS:
                value = value.log1p()
            exprs.append(value.shift(lag).over(["obj", "seg"]).alias(name))
            cols.append(name)
        if lag:
            name = f"seq_gap_{lag}"
            exprs.append((pl.col("day") - pl.col("day").shift(lag)
                          .over(["obj", "seg"])).dt.total_days().alias(name))
            cols.append(name)
    return frame.with_columns(exprs), cols


def score_block(block: pl.DataFrame, scores: np.ndarray, budget: int) -> dict:
    y = block["y"].to_numpy()
    d = block["day"].to_numpy()
    out = metrics.daily_budget_summary(y, scores, d, budget)
    return {"n": len(y), "positives": int(y.sum()),
            "pr_auc": metrics.pr_auc(y, scores),
            "daily_precision": out["daily_precision_at_k"],
            "daily_recall": out["daily_recall_at_k"]}


def main() -> None:
    cfg = serve.load_heads()["B"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    con.close()
    feat, cols = windows(store.read_slice("segment", START, END))
    data = feat.join(lab, on=["obj", "seg", "day"]).sort(
        ["day", "obj", "seg"])
    quiet = data.filter(pl.col("days_since_fire").is_null() |
                        (pl.col("days_since_fire") >= 7))
    train = quiet.filter(pl.col("day") <= TRAIN_END)
    test = quiet.filter(pl.col("day") > TRAIN_END)
    if train.is_empty() or test.is_empty():
        raise ValueError("missing normal train or future test rows")
    # No labels enter the fit. Sampling is fixed only to limit CPU/RAM.
    sample = train.sample(n=min(train.height, 100_000), seed=17)
    imputer = SimpleImputer(strategy="median", add_indicator=True)
    scaler = StandardScaler()
    x_train = scaler.fit_transform(imputer.fit_transform(
        sample.select(cols).to_numpy()))
    x_test = scaler.transform(imputer.transform(test.select(cols).to_numpy()))
    x_train = np.clip(x_train, -10, 10)
    x_test = np.clip(x_test, -10, 10)
    pca = PCA(n_components=8, random_state=17).fit(x_train)
    recon = pca.inverse_transform(pca.transform(x_test))
    anomaly_pca = np.mean((x_test - recon) ** 2, axis=1)
    forest = IsolationForest(n_estimators=100, max_samples=256,
                             random_state=17, n_jobs=4).fit(x_train)
    anomaly_if = -forest.decision_function(x_test)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"train_rows": sample.height, "features": cols, "folds": []}
    for split in splits:
        mask = ((test["day"].to_numpy() >= np.datetime64(split.test_start)) &
                (test["day"].to_numpy() <= np.datetime64(split.test_end)))
        block = test.filter(pl.Series(mask))
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, scores in (("pca", anomaly_pca),
                             ("isolation_forest", anomaly_if)):
            row[name] = score_block(block, scores[mask], cfg["budget_per_day"])
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    results["mean"] = {
        name: {metric: float(np.mean([r[name][metric] for r in results["folds"]]))
               for metric in ("pr_auc", "daily_precision", "daily_recall")}
        for name in ("pca", "isolation_forest")}
    out = PATHS.reports / "fire_unsupervised_experiment.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
