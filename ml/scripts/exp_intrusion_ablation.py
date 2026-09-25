"""Ablate groups of seven-day object-history features for head C.

These are exploratory 2025 folds already used to identify history as useful;
the comparison selects a smaller candidate, not a blind estimate of lift.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from exp_intrusion_sequence import with_history
from mkl import cv, db, labels, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
GROUPS = {
    "alarm_history": ("n_alarms", "n_intrusion"),
    "guard_history": ("n_arm", "n_disarm", "obj_armed",
                      "days_since_arm_event"),
    "quality_history": ("n_bad", "night_frac_mean", "gap_days"),
}


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
    full, names = with_history(base)
    frames = {name: full.drop([c for c in names if not any(
        c.startswith(f"seq_{signal}_") for signal in signals)])
        for name, signals in GROUPS.items()}
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"note": "exploratory feature selection on already used 2025 folds",
               "groups": {k: list(v) for k, v in GROUPS.items()},
               "variants": {}}
    for name, frame in frames.items():
        fit = train.run("C", frame, lab, splits,
                        params=train.params_for(cfg, "lgbm"),
                        budget_per_day=cfg["budget_per_day"], backend="lgbm")
        vals = [{"start": f["test_start"], "end": f["test_end"],
                 "pr_auc": f["pr_auc"],
                 "daily_precision": f["daily_precision_at_k"],
                 "daily_recall": f["daily_recall_at_k"]}
                for f in fit["folds"]]
        results["variants"][name] = {
            "folds": vals,
            "mean": {key: float(np.mean([f[key] for f in vals]))
                     for key in ("pr_auc", "daily_precision", "daily_recall")}}
        print(name, json.dumps(results["variants"][name]["mean"],
                               ensure_ascii=False), flush=True)
    (PATHS.reports / "intrusion_ablation_experiment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
