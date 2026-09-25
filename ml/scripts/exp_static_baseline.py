"""Бьёт ли модель простой список «кто падал чаще» — и насколько.

Негативный контроль показал, что у части голов почти вся различающая
способность держится на статической склонности сущности, а не на динамике: у
головы C перестановка меток по времени внутри объекта сохраняет 0.860 из 0.889.
Значит модель во многом воспроизводит ранжирование, которое у диспетчера и так
есть, и заявлять прогноз в таком случае было бы преувеличением.

Бейзлайн: доля позитивных суток сущности ЗА ПРОШЛОЕ, накопительно и строго до
текущих суток. Мерится на тех же фолдах и том же бюджете, что модель.
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import cv, metrics, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")


def historical_rate(lab: pl.DataFrame, ent: str) -> np.ndarray:
    """Частота позитивов сущности по прошлым суткам, без текущих.

    Сдвиг на сутки обязателен: включив текущие сутки, бейзлайн увидел бы
    собственный ответ.
    """
    d = lab.sort([ent, "day"])
    cum = (d.with_columns(
        pl.col("y").cum_sum().over(ent).alias("_c"),
        pl.int_range(pl.len()).over(ent).alias("_i"))
        .with_columns(
            ((pl.col("_c") - pl.col("y")) / (pl.col("_i") + 1e-9)).alias("rate")))
    return cum.sort([ent, "day"])["rate"].to_numpy(), cum.sort([ent, "day"])


def run(head: str, cfg: dict, window_start: dt.date) -> None:
    feats, lab = load(head, cfg, window_start)
    ent = next(k for k in ("ch", "obj", "seg") if k in lab.columns)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    out = train.run(head, feats, lab, splits,
                    params=train.params_for(cfg, train.default_backend()),
                    budget_per_day=cfg["budget_per_day"])

    _, cum = historical_rate(lab, ent)
    dcol = cum["day"].to_numpy()
    rate = cum["rate"].to_numpy()
    y = cum["y"].to_numpy()

    print(f"\n{head}  {cfg['title']}", flush=True)
    print(f"{'':<22}{'ROC в сутках':>14}{'точность@бюджет':>18}{'полнота':>10}",
          flush=True)
    m = out["mean"]
    print(f"{'модель':<22}{m['roc_auc_within_day']:>14.4f}"
          f"{m['precision_at_k']:>18.3f}{m['recall_at_k']:>10.3f}", flush=True)

    per_fold = []
    for s in splits:
        mask = ((dcol >= np.datetime64(s.test_start))
                & (dcol <= np.datetime64(s.test_end)))
        if not mask.any() or y[mask].sum() == 0:
            continue
        nd = (s.test_end - s.test_start).days + 1
        r = metrics.summary(y[mask], rate[mask], budget=cfg["budget_per_day"] * nd)
        r["roc_auc_within_day"] = metrics.roc_auc_within_day(
            dcol[mask], y[mask], rate[mask])
        per_fold.append(r)
    b = {k: float(np.nanmean([f[k] for f in per_fold]))
         for k in ("roc_auc_within_day", "precision_at_k", "recall_at_k")}
    print(f"{'история сущности':<22}{b['roc_auc_within_day']:>14.4f}"
          f"{b['precision_at_k']:>18.3f}{b['recall_at_k']:>10.3f}", flush=True)
    d = m["precision_at_k"] - b["precision_at_k"]
    print(f"{'прирост модели':<22}{m['roc_auc_within_day'] - b['roc_auc_within_day']:>+14.4f}"
          f"{d:>+18.3f}{m['recall_at_k'] - b['recall_at_k']:>+10.3f}", flush=True)


def main() -> None:
    heads = serve.load_heads()
    ws = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or ["C", "C_armed", "D", "A_link", "A_strict"]):
        run(head, heads[head], ws)


if __name__ == "__main__":
    main()
