"""Проверка головы на текущем фичесторе с конфигурацией из heads.yaml.

Используется для ответа на вопрос «дал ли новый набор признаков прирост»:
параметры и политика признаков берутся ровно те, с которыми пойдёт
финальное обучение.
"""
import datetime as dt
import sys

import polars as pl

from mkl import config, cv, db, experiments, labels, serve, store, train
from mkl.config import HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
TEST_DAYS = 90



def _choice() -> dict:
    return config.head_a_choice()


def load(head, cfg, window_start):
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    lab = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ? AND day <= ?",
        [window_start, TRAIN_END]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], window_start, TRAIN_END)
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(p) for p in drop)])
    return feats, lab


def main():
    heads = serve.load_heads()
    window_start = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or list(heads)):
        cfg = heads[head]
        feats, lab = load(head, cfg, window_start)
        if lab.is_empty() or lab["y"].sum() == 0:
            print(f"{head}: позитивов нет — пропуск", flush=True)
            continue
        days = sorted(lab["day"].unique().to_list())
        splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                                 embargo_days=cfg["embargo_days"])
        out = train.run(head, feats, lab, splits, params=cfg.get("params"),
                        budget_per_day=cfg["budget_per_day"])
        m = out["mean"]
        experiments.log({"head": head, "step": "B9",
                         "note": f"профиль дня недели, {feats.width} признаков",
                         "n_features": len(out["feature_names"]), **m})
        ok = "ТЗ" if (m.get("op_precision", 0) >= 0.7
                      and m.get("op_recall", 0) > 0.5) else "  "
        print(f"{head:9} {cfg['title']:28} норм={m.get('pr_auc_norm', float('nan')):.4f}  "
              f"PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
              f"P@R50={m.get('p_at_r50', float('nan')):.3f}  "
              f"maxP={m.get('op_precision', float('nan')):.3f}@R="
              f"{m.get('op_recall', float('nan')):.3f}  {ok}", flush=True)
        if out["model"] is not None:
            top = train.importance(out["model"], out["feature_names"], top=8)
            print("   топ-8: " + ", ".join(top["feature"]), flush=True)


if __name__ == "__main__":
    main()
