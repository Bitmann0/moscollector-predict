"""Негативный контроль: перемешанные метки.

Самая сильная доступная проверка на утечку. Если признаки честны, то при
разрушенной связи «признаки — метка» качество обязано упасть до уровня
случайного ранжирования: PR-AUC к базовой ставке, ROC-AUC к 0.5, lift к 1.

Метки перемешиваются ВНУТРИ суток, а не по всей выборке. Перемешивание по всей
выборке разрушило бы и временную структуру: доля позитивов меняется по годам, и
модель, выучившая «в 2021 позитивов больше», показала бы отрыв от случайного,
не имея никакой утечки. Перестановка внутри суток сохраняет посуточную базовую
ставку и рвёт ровно ту связь, которую мы проверяем.

Второе плечо — перемешивание внутри суток И сущности: оно сохраняет ещё и
профиль конкретного канала. Если качество держится на нём, значит модель живёт
не на динамике, а на том, что «этот канал вообще склонен падать».
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import cv, metrics, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

SEED = 20260920


def _shuffle_within(lab: pl.DataFrame, keys: list[str], seed: int) -> pl.DataFrame:
    """Перестановка y внутри групп: базовая ставка группы сохраняется."""
    rng = np.random.default_rng(seed)
    return (lab.with_columns(pl.Series("_r", rng.random(lab.height)))
               .with_columns(pl.col("y").sort_by("_r").over(keys).alias("y"))
               .drop("_r"))


def run(head: str, cfg: dict, window_start: dt.date) -> None:
    feats, lab = load(head, cfg, window_start)
    if lab.is_empty() or lab["y"].sum() == 0:
        print(f"{head}: позитивов нет — пропуск", flush=True)
        return
    ent = next((k for k in ("ch", "obj", "seg") if k in lab.columns), None)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    arms = {
        "настоящие метки": lab,
        "перемешано в сутках": _shuffle_within(lab, ["day"], SEED),
    }
    if ent:
        arms["перемешано в сутках и по сущности"] = _shuffle_within(
            lab, ["day", ent], SEED + 1)

    print(f"\n{head}  {cfg['title']}", flush=True)
    base = None
    for name, l in arms.items():
        out = train.run(head, feats, l, splits, params=cfg.get("params"),
                        budget_per_day=cfg["budget_per_day"])
        m = out["mean"]
        if base is None:
            base = m
        print(f"  {name:<34} ROC={m.get('roc_auc', float('nan')):.4f}  "
              f"PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
              f"база={m.get('base_rate', float('nan')):.4f}  "
              f"lift={m.get('lift_at_k', float('nan')):.2f}", flush=True)
    print("  ожидание для перемешанных: ROC около 0.5, PR-AUC около базовой "
          "ставки, lift около 1", flush=True)


def main() -> None:
    heads = serve.load_heads()
    ws = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or ["C", "A_link", "D", "A_strict"]):
        run(head, heads[head], ws)


if __name__ == "__main__":
    main()
