"""Small neural fusion of raw event timing and object telemetry for head C.

This MLP sees the same event+daily-history features as the tree comparison,
with last-event token classes one-hot encoded. It is not the APT Transformer.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from scipy.stats import rankdata

from exp_intrusion_event_fusion import event_day
from exp_intrusion_sequence import with_history
from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)


def daily_rank(scores: np.ndarray, days: np.ndarray) -> np.ndarray:
    """Percentile rank within each day's fixed candidate set (low to high)."""
    out = np.empty(len(scores), dtype=float)
    _, groups = np.unique(days, return_inverse=True)
    for group in np.unique(groups):
        ix = np.flatnonzero(groups == group)
        out[ix] = (rankdata(scores[ix], method="average") - 0.5) / len(ix)
    return out


def score(y: np.ndarray, p: np.ndarray, days: np.ndarray, budget: int) -> dict:
    daily = metrics.daily_budget_summary(y, p, days, budget)
    return {"pr_auc": metrics.pr_auc(y, p),
            "daily_precision": daily["daily_precision_at_k"],
            "daily_recall": daily["daily_recall_at_k"]}


def main() -> None:
    cfg = serve.load_heads()["C"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    con.close()
    base = store.read_slice("object", START, END)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    event = pl.concat([event_day(y) for y in (2023, 2024, 2025)])
    history, _ = with_history(base)
    history_data = history.join(lab, on=["obj", "day"]).sort(["day", "obj"])
    fused = base.join(event, on=["obj", "day"], how="left")
    count_cols = [c for c in event.columns if c.startswith("evt_") and
                  not c.startswith("evt_last_") and c != "evt_intrusion_span_s"]
    fused = fused.with_columns([pl.col(c).fill_null(0) for c in count_cols])
    fused, _ = with_history(fused)
    token_cols = [f"evt_last_token_{i}" for i in (1, 2, 3)]
    exprs = [(pl.col(col) == value).fill_null(False).cast(pl.Int8)
             .alias(f"{col}_is_{value}")
             for col in token_cols for value in (1, 2, 3, 4)]
    fused = fused.with_columns(exprs).drop(token_cols)
    data = fused.join(lab, on=["obj", "day"]).sort(["day", "obj"])
    cols = train.feature_columns(data)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"note": "small MLP, not APT; 2025 folds used before",
               "folds": []}
    for split in splits:
        tr = data.filter((pl.col("day") >= split.train_start) &
                         (pl.col("day") <= split.train_end))
        te = data.filter((pl.col("day") >= split.test_start) &
                         (pl.col("day") <= split.test_end))
        imputer = SimpleImputer(strategy="median", add_indicator=True)
        scaler = StandardScaler()
        xtr = scaler.fit_transform(imputer.fit_transform(train._matrix(tr, cols)))
        xte = scaler.transform(imputer.transform(train._matrix(te, cols)))
        xtr, xte = np.clip(xtr, -10, 10), np.clip(xte, -10, 10)
        model = MLPClassifier(hidden_layer_sizes=(128, 64), batch_size=512,
                              max_iter=50, early_stopping=True,
                              n_iter_no_change=7, random_state=17)
        model.fit(xtr, tr["y"].to_numpy())
        p = model.predict_proba(xte)[:, 1]
        y, days = te["y"].to_numpy(), te["day"].to_numpy()
        tree = train.run("C", history, lab, [split],
                         params=train.params_for(cfg, "lgbm"),
                         budget_per_day=cfg["budget_per_day"], backend="lgbm")
        tree_block = history_data.filter(
            (pl.col("day") >= split.test_start) &
            (pl.col("day") <= split.test_end))
        if not np.array_equal(te["obj"].to_numpy(),
                              tree_block["obj"].to_numpy()) or not np.array_equal(
                                  days, tree_block["day"].to_numpy()):
            raise ValueError("candidate order mismatch")
        p_tree = tree["model"].predict_proba(
            train._matrix(tree_block, tree["feature_names"]))[:, 1]
        blend = (daily_rank(p, days) + daily_rank(p_tree, days)) / 2
        row = {"start": str(split.test_start), "end": str(split.test_end),
               "n": len(y), "positives": int(y.sum()), "epochs": model.n_iter_,
               "mlp": score(y, p, days, cfg["budget_per_day"]),
               "history_tree": score(y, p_tree, days, cfg["budget_per_day"]),
               "fixed_rank_blend": score(y, blend, days, cfg["budget_per_day"])}
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    results["mean"] = {
        variant: {key: float(np.mean([r[variant][key] for r in results["folds"]]))
                  for key in ("pr_auc", "daily_precision", "daily_recall")}
        for variant in ("mlp", "history_tree", "fixed_rank_blend")}
    (PATHS.reports / "intrusion_event_mlp_experiment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
