"""First supervised NN check on week-long causal sensor sequences.

The same sequence-augmented rows are given to LightGBM and an MLP. Both are
evaluated on quiet-week candidates in three pre-2026 walk-forward folds.
This does not yet use the full raw event stream or a temporal encoder.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

from exp_fire_unsupervised import windows
from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
KEYS = ["obj", "seg", "day"]


def evaluate(y: np.ndarray, p: np.ndarray, days: np.ndarray,
             budget: int) -> dict:
    got = metrics.daily_budget_summary(y, p, days, budget)
    return {"n": len(y), "positives": int(y.sum()),
            "pr_auc": metrics.pr_auc(y, p),
            "daily_precision": got["daily_precision_at_k"],
            "daily_recall": got["daily_recall_at_k"]}


def main() -> None:
    cfg = serve.load_heads()["B"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    con.close()
    base = store.read_slice("segment", START, END)
    base = base.select([c for c in base.columns if not c.startswith("sensor_")])
    frame, sequence_cols = windows(base)
    quiet = frame.filter(pl.col("days_since_fire").is_null() |
                         (pl.col("days_since_fire") >= 7))
    data = quiet.join(lab, on=KEYS).sort(["day", "obj", "seg"])
    cols = train.feature_columns(data)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"sequence_features": sequence_cols, "n_features": len(cols),
               "folds": []}
    for split in splits:
        train_df = data.filter((pl.col("day") >= split.train_start) &
                               (pl.col("day") <= split.train_end))
        test_df = data.filter((pl.col("day") >= split.test_start) &
                              (pl.col("day") <= split.test_end))
        ytr = train_df["y"].to_numpy()
        yte = test_df["y"].to_numpy()
        if ytr.sum() == 0 or yte.sum() == 0:
            raise ValueError(f"no positives in {split.test_start} fold")
        xtr = train._matrix(train_df, cols)
        xte = train._matrix(test_df, cols)
        imputer = SimpleImputer(strategy="median", add_indicator=True)
        scaler = StandardScaler()
        xtr = scaler.fit_transform(imputer.fit_transform(xtr))
        xte = scaler.transform(imputer.transform(xte))
        xtr = np.clip(xtr, -10, 10)
        xte = np.clip(xte, -10, 10)
        pos_weight = min(100.0, (len(ytr) - ytr.sum()) / ytr.sum())
        weights = np.where(ytr == 1, pos_weight, 1.0)
        mlp = MLPClassifier(hidden_layer_sizes=(64, 32),
                            batch_size=1024, max_iter=25,
                            early_stopping=True, n_iter_no_change=5,
                            random_state=17)
        mlp.fit(xtr, ytr, sample_weight=weights)
        p_mlp = mlp.predict_proba(xte)[:, 1]
        tree = train.run("B", quiet, lab, [split],
                         params=train.params_for(cfg, "lgbm"),
                         budget_per_day=cfg["budget_per_day"], backend="lgbm")
        p_tree = tree["model"].predict_proba(
            train._matrix(test_df, tree["feature_names"]))[:, 1]
        days = test_df["day"].to_numpy()
        row = {"start": str(split.test_start), "end": str(split.test_end),
               "n_train": len(ytr), "n_positive_train": int(ytr.sum()),
               "mlp_epochs": mlp.n_iter_,
               "mlp": evaluate(yte, p_mlp, days, cfg["budget_per_day"]),
               "lightgbm": evaluate(yte, p_tree, days, cfg["budget_per_day"])}
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    results["mean"] = {
        name: {metric: float(np.mean([r[name][metric] for r in results["folds"]]))
               for metric in ("pr_auc", "daily_precision", "daily_recall")}
        for name in ("mlp", "lightgbm")}
    out = PATHS.reports / "fire_sequence_mlp_experiment.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
