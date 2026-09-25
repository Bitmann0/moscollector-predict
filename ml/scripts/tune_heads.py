"""Догон голов, стоящих у границы требований ТЗ.

Голова A тюнится на ПОБЕДИВШЕМ наборе признаков (B3 — без peer и
пространственных): в лестнице долгое обучение досталось полному набору,
включая признаки, уже отклонённые замером, и потому проиграло.
Голова C (несанкционированный доступ) не дотянула до цели тысячные доли.
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import config, cv, db, experiments, labels, serve, store, train
from mkl.config import HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
TEST_DAYS = 90

GRID = [
    ("1200x0.02 leaves127", {"n_estimators": 1200, "learning_rate": 0.02,
                             "num_leaves": 127}),
    ("800x0.03 leaves255", {"n_estimators": 800, "learning_rate": 0.03,
                            "num_leaves": 255, "min_child_samples": 200}),
    ("600x0.05 depth8", {"n_estimators": 600, "learning_rate": 0.05,
                         "num_leaves": 63, "max_depth": 8}),
    ("400x0.05 colsample0.6", {"n_estimators": 400, "learning_rate": 0.05,
                               "colsample_bytree": 0.6, "min_child_samples": 300}),
]


def choice() -> dict:
    return config.head_a_choice()


def drop_rejected(feats: pl.DataFrame) -> pl.DataFrame:
    """Убрать признаки, отклонённые ступенями B4 и B5."""
    peer = [c for c in feats.columns if c.startswith("peer_")]
    spat = [c for c in feats.columns
            if c.startswith("nbr_") or c == "val_minus_seg_mean"]
    return feats.select([c for c in feats.columns if c not in peer + spat])


def labels_for(head: str, cfg: dict, window_start: dt.date) -> pl.DataFrame:
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    if head == "A":
        c = choice()
        labels.build_sensor_failure(con, variant=c["variant"],
                                    horizon_days=int(c["horizon_days"]))
    elif head == "C":
        labels.build_intrusion(con, horizon_days=cfg["horizon_days"])
    else:
        raise ValueError(head)
    df = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ? AND day <= ?",
        [window_start, TRAIN_END],
    ).pl()
    con.close()
    return df


def tune(head: str) -> None:
    cfg = serve.load_heads()[head]
    window_start = dt.date.fromisoformat(choice()["window_start"])
    lab = labels_for(head, cfg, window_start)
    feats = store.read_slice(cfg["feature_set"], window_start, TRAIN_END)
    if head == "A":
        feats = drop_rejected(feats)

    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    print(f"\n=== {head}: {cfg['title']} — {feats.width} признаков, "
          f"{lab['y'].sum():,} позитивов ===", flush=True)

    for note, params in GRID:
        out = train.run(head, feats, lab, splits, params=params,
                        budget_per_day=cfg["budget_per_day"])
        m = out["mean"]
        experiments.log({"head": head, "step": "B6", "note": f"тюнинг {note}",
                         "n_features": len(out["feature_names"]), **m})
        ok = "ТЗ" if (m.get("op_precision", 0) >= 0.7
                      and m.get("op_recall", 0) > 0.5) else "  "
        print(f"  {note:24} норм={m.get('pr_auc_norm', float('nan')):.4f}  "
              f"PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
              f"P@R50={m.get('p_at_r50', float('nan')):.3f}  "
              f"maxP={m.get('op_precision', float('nan')):.3f}@R="
              f"{m.get('op_recall', float('nan')):.3f}  {ok}", flush=True)


def main() -> None:
    for head in ("C", "A"):
        tune(head)
    print("\n=== журнал после тюнинга ===", flush=True)
    print(experiments.table().to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
