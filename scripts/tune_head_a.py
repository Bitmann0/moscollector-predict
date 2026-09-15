"""Догон головы A: долгое обучение и подбор гиперпараметров на ПОБЕДИВШЕМ
наборе признаков (B3 — без peer и пространственных), а не на полном.

В первом прогоне ступень B7 обучалась на всех признаках, включая отклонённые
замером, и проиграла более короткому обучению на правильном наборе.
"""
import datetime as dt
import sys

import polars as pl

from mkl import cv, db, experiments, labels, store, train
from mkl.config import EMBARGO_DAYS, HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
TEST_DAYS = 90
BUDGET_PER_DAY = 20

GRID = [
    ("B6 1200x0.02 leaves127", {"n_estimators": 1200, "learning_rate": 0.02,
                                "num_leaves": 127}),
    ("B6 800x0.03 leaves255", {"n_estimators": 800, "learning_rate": 0.03,
                               "num_leaves": 255, "min_child_samples": 200}),
    ("B6 600x0.05 depth8", {"n_estimators": 600, "learning_rate": 0.05,
                            "num_leaves": 63, "max_depth": 8}),
    ("B6 400x0.05 colsample0.6", {"n_estimators": 400, "learning_rate": 0.05,
                                  "colsample_bytree": 0.6, "min_child_samples": 300}),
]


def choice() -> dict:
    out = {"window_start": "2019-01-01", "variant": "L5",
           "horizon_days": "1", "eligible_only": "False"}
    path = PATHS.reports / "head_a_choice.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def main() -> None:
    c = choice()
    window_start = dt.date.fromisoformat(c["window_start"])
    horizon = int(c["horizon_days"])
    print(f"конфигурация головы A: окно {window_start}, метка {c['variant']}, "
          f"горизонт {horizon} сут", flush=True)

    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_sensor_failure(con, variant=c["variant"], horizon_days=horizon)
    lab = con.execute(
        "SELECT ch, day, y FROM label_failure WHERE day >= ? AND day <= ?",
        [window_start, TRAIN_END],
    ).pl()
    con.close()

    feats = store.read_slice("sensor", window_start, TRAIN_END)
    peer_cols = [x for x in feats.columns if x.startswith("peer_")]
    spat_cols = [x for x in feats.columns
                 if x.startswith("nbr_") or x == "val_minus_seg_mean"]
    base_cols = [x for x in feats.columns if x not in peer_cols + spat_cols]
    feats = feats.select(base_cols)
    print(f"признаков в наборе B3: {len(base_cols)}", flush=True)

    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=EMBARGO_DAYS)

    for note, params in GRID:
        out = train.run("A", feats, lab, splits, params=params,
                        budget_per_day=BUDGET_PER_DAY)
        m = out["mean"]
        experiments.log({"head": "A", "step": "B6", "note": note,
                         "n_features": len(out["feature_names"]), **m})
        print(f"  {note:28} норм={m.get('pr_auc_norm', float('nan')):.4f}  "
              f"PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
              f"P@R50={m.get('p_at_r50', float('nan')):.3f}  "
              f"maxP={m.get('op_precision', float('nan')):.3f}@R="
              f"{m.get('op_recall', float('nan')):.3f}", flush=True)

    print("\n=== журнал головы A ===", flush=True)
    print(experiments.table("A").to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
