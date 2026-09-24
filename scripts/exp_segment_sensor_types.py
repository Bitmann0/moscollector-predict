"""Walk-forward ablation of type-specific numeric features for fire risk.

Uses only dates before the repeatedly viewed 2026 evaluation period. Baseline
and candidate share rows, labels, splits, model parameters, and alert budget.
"""
import datetime as dt
import json
import sys

from mkl import cv, db, labels, serve, store, train
from mkl.config import HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

START = dt.date(2023, 1, 1)
END = HOLDOUT_START - dt.timedelta(days=1)


def main() -> None:
    cfg = serve.load_heads()["B"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con, horizon_days=cfg["horizon_days"])
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], START, END)
    baseline = feats.select([c for c in feats.columns if not c.startswith("sensor_")])
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=90,
                             embargo_days=cfg["embargo_days"])
    backend = "lgbm"  # CPU makes this reproducible on machines without CUDA.
    params = train.params_for(cfg, backend)
    results = {}
    for name, frame in (("baseline", baseline), ("typed", feats)):
        out = train.run("B", frame, lab, splits, params=params,
                        budget_per_day=cfg["budget_per_day"], backend=backend)
        results[name] = {
            "n_features": len(out["feature_names"]),
            "mean": {k: out["mean"].get(k) for k in
                     ("pr_auc", "pr_auc_norm", "roc_auc", "precision_at_k",
                      "recall_at_k", "daily_precision_at_k", "daily_recall_at_k",
                      "global_days_over_budget", "episode_recall",
                      "op_precision", "op_recall", "n_pos", "n")},
            "folds": [{k: fold.get(k) for k in
                       ("test_start", "test_end", "pr_auc", "pr_auc_norm",
                        "precision_at_k", "recall_at_k", "daily_precision_at_k",
                        "daily_recall_at_k", "global_days_over_budget",
                        "episode_recall", "n_pos", "n")}
                      for fold in out["folds"]],
        }
        print(name, json.dumps(results[name]["mean"], ensure_ascii=False), flush=True)
    path = PATHS.reports / "segment_sensor_types_ablation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"backend": backend, "start": str(START),
                                "end": str(END), "results": results},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(path, flush=True)


if __name__ == "__main__":
    main()
