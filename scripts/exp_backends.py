"""Ступень B8: бьёт ли другое семейство бустинга LightGBM.

XGBoost и CatBoost считаются на GPU (RTX 3060 Ti), поэтому перебор семейств
стоит примерно столько же, сколько одна конфигурация на CPU.
"""
import datetime as dt
import sys
import time

import polars as pl

from mkl import cv, db, experiments, labels, serve, store, train
from mkl.config import HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
TEST_DAYS = 90

BACKENDS = [
    ("lgbm", "LightGBM CPU", {"n_estimators": 1200, "learning_rate": 0.02,
                              "num_leaves": 127}),
    ("xgb", "XGBoost GPU", {"n_estimators": 1200, "learning_rate": 0.02,
                            "max_depth": 9}),
    ("cat", "CatBoost GPU", {"iterations": 1200, "learning_rate": 0.02,
                             "depth": 9}),
]

BUILDERS = {
    "A": lambda con, h: labels.build_sensor_failure(con, variant=_choice()["variant"],
                                                    horizon_days=h),
    "A_strict": labels.build_sensor_failure_strict,
    "A_deg": labels.build_sensor_degradation,
    "A_prime": labels.build_group_outage,
    "B": labels.build_fire,
    "C": labels.build_intrusion,
    "D": labels.build_wear,
}


def _choice() -> dict:
    out = {"window_start": "2019-01-01", "variant": "L5", "horizon_days": "1"}
    path = PATHS.reports / "head_a_choice.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def load(head: str, cfg: dict, window_start: dt.date):
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    b = BUILDERS[head]
    if head == "A":
        b(con, cfg["horizon_days"])
    else:
        b(con, horizon_days=cfg["horizon_days"])
    lab = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ? AND day <= ?",
        [window_start, TRAIN_END],
    ).pl()
    con.close()

    feats = store.read_slice(cfg["feature_set"], window_start, TRAIN_END)
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select(
            [c for c in feats.columns if not any(c.startswith(p) for p in drop)])
    return feats, lab


def main() -> None:
    heads = serve.load_heads()
    window_start = dt.date.fromisoformat(_choice()["window_start"])
    only = sys.argv[1:] or list(heads)

    for head in only:
        cfg = heads[head]
        feats, lab = load(head, cfg, window_start)
        if lab.is_empty() or lab["y"].sum() == 0:
            print(f"{head}: позитивов нет — пропуск", flush=True)
            continue
        days = sorted(lab["day"].unique().to_list())
        splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                                 embargo_days=cfg["embargo_days"])
        print(f"\n=== {head}: {cfg['title']} — {feats.width} признаков, "
              f"{lab['y'].sum():,} позитивов ===", flush=True)

        for backend, title, params in BACKENDS:
            t0 = time.time()
            try:
                out = train.run(head, feats, lab, splits, params=params,
                                budget_per_day=cfg["budget_per_day"],
                                backend=backend)
            except Exception as exc:
                print(f"  {title:16} ОШИБКА: {type(exc).__name__}: "
                      f"{str(exc)[:120]}", flush=True)
                continue
            m = out["mean"]
            experiments.log({"head": head, "step": "B8", "backend": backend,
                             "note": f"{title} 1200 деревьев",
                             "fit_seconds": round(time.time() - t0, 1),
                             "n_features": len(out["feature_names"]), **m})
            ok = "ТЗ" if (m.get("op_precision", 0) >= 0.7
                          and m.get("op_recall", 0) > 0.5) else "  "
            print(f"  {title:16} норм={m.get('pr_auc_norm', float('nan')):.4f}  "
                  f"PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
                  f"P@R50={m.get('p_at_r50', float('nan')):.3f}  "
                  f"maxP={m.get('op_precision', float('nan')):.3f}@R="
                  f"{m.get('op_recall', float('nan')):.3f}  {ok}  "
                  f"[{time.time() - t0:.0f}s]", flush=True)


if __name__ == "__main__":
    main()
