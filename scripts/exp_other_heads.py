"""Головы A′ (массовый отказ объекта), B (пожарный риск участка),
C (несанкционированный доступ) и D (износ агрегатов).
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import cv, db, experiments, labels, metrics, serve, store, train
from mkl.config import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
WINDOW_START = dt.date(2023, 1, 1)

BUILDERS = {
    "A_prime": labels.build_group_outage,
    "B": labels.build_fire,
    "C": labels.build_intrusion,
    "D": labels.build_wear,
}


def load_labels(head: str, cfg: dict) -> pl.DataFrame:
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    BUILDERS[head](con, horizon_days=cfg["horizon_days"])
    df = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ? AND day <= ?",
        [WINDOW_START, TRAIN_END],
    ).pl()
    con.close()
    return df


def run_head(head: str, cfg: dict) -> None:
    lab = load_labels(head, cfg)
    if lab.is_empty() or lab["y"].sum() == 0:
        print(f"{head:8} позитивов нет — пропуск", flush=True)
        return

    feats = store.read_slice(cfg["feature_set"], WINDOW_START, TRAIN_END)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=30,
                             embargo_days=cfg["embargo_days"])
    n_days = sum((s.test_end - s.test_start).days + 1 for s in splits) // len(splits)
    budget = cfg["budget_per_day"] * n_days

    join_keys = [k for k in ("ch", "obj", "seg", "day")
                 if k in feats.columns and k in lab.columns]
    joined = feats.join(lab, on=join_keys, how="inner")
    y = joined["y"].to_numpy()

    # B1: текущая практика — скор по активности за прошлую неделю
    rule_col = next((c for c in ("n_alarms_w7", "n_alarms") if c in joined.columns), None)
    if rule_col:
        b1 = metrics.summary(
            y, joined[rule_col].fill_null(0).cast(pl.Float64).to_numpy(), budget=budget
        )
        experiments.log({"head": head, "step": "B1",
                         "note": f"правило ОДС: {rule_col}", **b1})
        print(f"{head:8} B1 {'правило ОДС':26} PR-AUC={b1['pr_auc']:.4f}  "
              f"P@k={b1['precision_at_k']:.3f}  R@k={b1['recall_at_k']:.3f}", flush=True)
    del joined

    out = train.run(head, feats, lab, splits, budget_per_day=cfg["budget_per_day"])
    m = out["mean"]
    experiments.log({"head": head, "step": "B5", "note": cfg["title"],
                     "n_features": len(out["feature_names"]), **m})
    print(f"{head:8} B5 {cfg['title']:26} PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
          f"P@k={m.get('precision_at_k', float('nan')):.3f}  "
          f"R@k={m.get('recall_at_k', float('nan')):.3f}  "
          f"lift={m.get('lift_at_k', float('nan')):.1f}  "
          f"позитивов={m.get('n_pos', 0):,}", flush=True)

    if out["model"] is not None:
        print("   топ-10 признаков: " + ", ".join(
            train.importance(out["model"], out["feature_names"], top=10)["feature"]
        ), flush=True)


def main() -> None:
    heads = serve.load_heads()
    for head in ("A_prime", "B", "C", "D"):
        print(flush=True)
        run_head(head, heads[head])

    print("\n=== сводка по всем головам ===", flush=True)
    print(experiments.table().to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
