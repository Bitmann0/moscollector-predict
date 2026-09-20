"""Ранжирующая постановка против классификации.

Отчитываемся точностью на бюджете — качеством верхушки суточного списка, — а
обучаем посуточную классификацию, которой безразлично, как соотносятся сущности
внутри одних суток. lambdarank с группировкой по суткам оптимизирует ровно то,
что видит диспетчер.

Плечи сравниваются по согласию знака на фолдах, а не по среднему: разброс между
запусками доходил до 14% относительных, и средним такую дельту не закрыть.
"""
import datetime as dt
import sys

import lightgbm as lgb
import numpy as np
import polars as pl

from mkl import cv, metrics, serve, store, train
from mkl.config import EXCLUDED_PERIODS, HOLDOUT_START
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

RANK_PARAMS = {
    "objective": "lambdarank",
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 127,
    "min_child_samples": 50,
    "max_bin": 63,
    "verbose": -1,
    "n_jobs": 8,
    "random_state": 42,
    # Усечение метрики на бюджете: дальше него список диспетчер не смотрит.
    "lambdarank_truncation_level": 30,
}


def _prep(feats, lab):
    jk = [k for k in ("ch", "obj", "seg", "day")
          if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=jk, how="inner").sort(jk)
    for a, b in EXCLUDED_PERIODS:
        data = data.filter((pl.col("day") < a) | (pl.col("day") > b))
    cols = train.feature_columns(data)
    return data, cols, train._matrix(data, cols)


def run(head: str, cfg: dict, window_start: dt.date) -> None:
    feats, lab = load(head, cfg, window_start)
    data, cols, X = _prep(feats, lab)
    y = data["y"].to_numpy()
    days = data["day"].to_numpy()
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), n_splits=3,
                             test_days=TEST_DAYS, embargo_days=cfg["embargo_days"])
    print(f"\n{head}  {cfg['title']}  ({len(y):,} строк, {int(y.sum()):,} позитивов)",
          flush=True)
    res = {"классификация": [], "ранжирование": []}
    for s in splits:
        tr = (days >= np.datetime64(s.train_start)) & (days <= np.datetime64(s.train_end))
        te = (days >= np.datetime64(s.test_start)) & (days <= np.datetime64(s.test_end))
        if y[tr].sum() == 0 or y[te].sum() == 0:
            continue
        nd = (s.test_end - s.test_start).days + 1
        budget = cfg["budget_per_day"] * nd

        pos = int(y[tr].sum())
        clf = train._build_model("lgbm", cfg.get("params"), (len(y[tr]) - pos) / pos)
        clf.fit(X[tr], y[tr])
        res["классификация"].append(
            metrics.summary(y[te], clf.predict_proba(X[te])[:, 1], budget=budget))

        # Группы для lambdarank: сущности одних суток соревнуются между собой.
        d_tr = days[tr]
        _, counts = np.unique(d_tr, return_counts=True)
        rk = lgb.LGBMRanker(**RANK_PARAMS)
        rk.fit(X[tr], y[tr], group=counts)
        res["ранжирование"].append(
            metrics.summary(y[te], rk.predict(X[te]), budget=budget))

    if not res["классификация"]:
        print("  фолдов нет", flush=True)
        return
    for key in ("precision_at_k", "recall_at_k", "roc_auc"):
        a = [f[key] for f in res["классификация"]]
        b = [f[key] for f in res["ранжирование"]]
        d = [y2 - y1 for y1, y2 in zip(a, b)]
        won = sum(v > 0 for v in d)
        print(f"  {key:<14} класс={np.mean(a):.4f}  ранж={np.mean(b):.4f}  "
              f"дельта по фолдам " + " ".join(f"{v:+.3f}" for v in d)
              + f"   выиграно {won}/{len(d)}", flush=True)


def main() -> None:
    heads = serve.load_heads()
    ws = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or ["C", "A_link", "D"]):
        run(head, heads[head], ws)


if __name__ == "__main__":
    main()
