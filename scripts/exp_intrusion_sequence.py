"""Object-history experiment for the armed intrusion-alarm proxy.

Compare the current LightGBM policy, LightGBM with seven observed-day lags,
and an MLP on the same lag-augmented features. Nothing is deployed here.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
SIGNALS = ("n_alarms", "n_intrusion", "n_arm", "n_disarm", "obj_armed",
           "days_since_arm_event", "n_bad", "night_frac_mean")


def with_history(frame: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    frame = frame.sort(["obj", "day"])
    exprs = []
    names = []
    for lag in range(1, 8):
        for signal in SIGNALS:
            name = f"seq_{signal}_{lag}"
            exprs.append(pl.col(signal).shift(lag).over("obj").alias(name))
            names.append(name)
        name = f"seq_gap_days_{lag}"
        exprs.append((pl.col("day") - pl.col("day").shift(lag)
                      .over("obj")).dt.total_days().alias(name))
        names.append(name)
    return frame.with_columns(exprs), names


def score(y: np.ndarray, p: np.ndarray, days: np.ndarray, budget: int) -> dict:
    out = metrics.daily_budget_summary(y, p, days, budget)
    return {"n": len(y), "positives": int(y.sum()),
            "pr_auc": metrics.pr_auc(y, p),
            "daily_precision": out["daily_precision_at_k"],
            "daily_recall": out["daily_recall_at_k"]}


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
    extended, sequence_names = with_history(base)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    data = extended.join(lab, on=["obj", "day"]).sort(["day", "obj"])
    cols = train.feature_columns(data)
    results = {"sequence_features": sequence_names, "folds": []}
    for split in splits:
        tr = data.filter((pl.col("day") >= split.train_start) &
                         (pl.col("day") <= split.train_end))
        te = data.filter((pl.col("day") >= split.test_start) &
                         (pl.col("day") <= split.test_end))
        ytr, yte = tr["y"].to_numpy(), te["y"].to_numpy()
        days = te["day"].to_numpy()
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, frame in (("baseline", base), ("history_tree", extended)):
            fit = train.run("C", frame, lab, [split],
                            params=train.params_for(cfg, "lgbm"),
                            budget_per_day=cfg["budget_per_day"], backend="lgbm")
            if fit["model"] is None:
                raise ValueError(f"{name}: no model")
            p = fit["model"].predict_proba(
                train._matrix(te, fit["feature_names"]))[:, 1]
            row[name] = score(yte, p, days, cfg["budget_per_day"])
        imputer = SimpleImputer(strategy="median", add_indicator=True)
        scaler = StandardScaler()
        xtr = scaler.fit_transform(imputer.fit_transform(train._matrix(tr, cols)))
        xte = scaler.transform(imputer.transform(train._matrix(te, cols)))
        xtr, xte = np.clip(xtr, -10, 10), np.clip(xte, -10, 10)
        mlp = MLPClassifier(hidden_layer_sizes=(64, 32), batch_size=512,
                            max_iter=40, early_stopping=True,
                            n_iter_no_change=6, random_state=17)
        mlp.fit(xtr, ytr)
        p_mlp = mlp.predict_proba(xte)[:, 1]
        row["history_mlp"] = score(yte, p_mlp, days, cfg["budget_per_day"])
        row["mlp_epochs"] = mlp.n_iter_
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    results["mean"] = {
        name: {metric: float(np.mean([r[name][metric] for r in results["folds"]]))
               for metric in ("pr_auc", "daily_precision", "daily_recall")}
        for name in ("baseline", "history_tree", "history_mlp")}
    out = PATHS.reports / "intrusion_sequence_experiment.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
