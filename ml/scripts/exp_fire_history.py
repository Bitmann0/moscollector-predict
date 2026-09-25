"""Ablation of causal segment fire history on three pre-2026 folds."""
import datetime as dt
import json
import sys

from mkl import cv, db, labels, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    cfg = serve.load_heads()["B"]
    start, end = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [start, end]).pl()
    con.close()
    feat = store.read_slice("segment", start, end)
    base = feat.select([c for c in feat.columns
                        if not c.startswith(("sensor_", "fire_", "days_since_fire"))])
    history = feat.select([c for c in feat.columns if not c.startswith("sensor_")])
    rate_only = history.drop("days_since_fire")
    recency_only = history.drop("fire_days_to_date", "fire_rate_to_date")
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), n_splits=3,
                             test_days=90, embargo_days=cfg["embargo_days"])
    results = {}
    for name, frame in (("baseline", base), ("rate_only", rate_only),
                        ("recency_only", recency_only), ("history", history)):
        fit = train.run("B", frame, lab, splits,
                        params=train.params_for(cfg, "lgbm"),
                        budget_per_day=cfg["budget_per_day"], backend="lgbm")
        keys = ("pr_auc", "roc_auc", "daily_precision_at_k",
                "daily_recall_at_k", "episode_recall")
        results[name] = {
            "n_features": len(fit["feature_names"]),
            "mean": {key: fit["mean"][key] for key in keys},
            "folds": [{"start": f["test_start"], "end": f["test_end"],
                       **{key: f[key] for key in keys}} for f in fit["folds"]],
        }
        print(name, json.dumps(results[name]["mean"]), flush=True)
    (PATHS.reports / "fire_history_ablation.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
