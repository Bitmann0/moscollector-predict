"""Три решения, принятых до аудита и с тех пор не пересматривавшихся.

Окно обучения и гиперпараметры выбирались на метках, которые аудит признал
неверными: групповой отказ на 92.7% состоял из дребезга, а метка головы A на
98.89% из молчания. Решение, оптимальное для той постановки, для нынешней
оптимальным быть не обязано.

Затухание по свежести не используется ни одной головой, хотя парк за 7.5 лет
вырос с 5 622 каналов до 11 483: строки 2019 года описывают другую систему.

Критерий — точность на бюджете, согласие знака по фолдам.
"""
import datetime as dt
import sys

import polars as pl

from mkl import cv, experiments, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

WINDOWS = ("2019-01-01", "2021-07-01", "2023-01-01", "2024-01-01")
HALF_LIVES = (None, 730.0, 365.0, 180.0)


def _run(head, cfg, feats, lab, splits, **kw):
    return train.run(head, feats, lab, splits,
                     params=train.params_for(cfg, train.default_backend()),
                     budget_per_day=cfg["budget_per_day"], **kw)


def _line(tag, out, ref=None):
    m = out["mean"]
    s = (f"  {tag:<24} P@k={m['precision_at_k']:.4f}  R@k={m['recall_at_k']:.4f}"
         f"  ROC={m['roc_auc']:.4f}")
    if ref is not None:
        d = [b["precision_at_k"] - a["precision_at_k"]
             for a, b in zip(ref["folds"], out["folds"])]
        s += ("  дельта " + " ".join(f"{v:+.3f}" for v in d)
              + f"  {sum(v > 0 for v in d)}/{len(d)}")
    print(s, flush=True)


def windows(head: str, cfg: dict) -> None:
    print(f"\n{head} — окно обучения", flush=True)
    ref = None
    for w in WINDOWS:
        ws = dt.date.fromisoformat(w)
        feats, lab = load(head, cfg, ws)
        days = sorted(lab["day"].unique().to_list())
        splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                                 embargo_days=cfg["embargo_days"])
        if not splits:
            print(f"  {w:<24} окно короче протокола — пропуск", flush=True)
            continue
        out = _run(head, cfg, feats, lab, splits)
        experiments.log({"head": head, "step": "F1", "note": f"окно с {w}",
                         **out["mean"]})
        _line(f"с {w}", out, ref if w != WINDOWS[0] else None)
        if ref is None:
            ref = out


def recency(head: str, cfg: dict) -> None:
    print(f"\n{head} — вес по свежести", flush=True)
    ws = dt.date.fromisoformat(_choice()["window_start"])
    feats, lab = load(head, cfg, ws)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    ref = None
    for hl in HALF_LIVES:
        out = _run(head, cfg, feats, lab, splits, half_life_days=hl)
        experiments.log({"head": head, "step": "F2",
                         "note": f"полураспад {hl or 'нет'}", **out["mean"]})
        tag = "без затухания" if hl is None else f"полураспад {hl:.0f} сут"
        _line(tag, out, ref)
        if ref is None:
            ref = out


def main() -> None:
    heads = serve.load_heads()
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = sys.argv[2:] or ["C", "A_link", "D"]
    for head in names:
        cfg = heads[head]
        if what in ("all", "window"):
            windows(head, cfg)
        if what in ("all", "recency"):
            recency(head, cfg)


if __name__ == "__main__":
    main()
