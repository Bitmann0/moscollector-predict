"""Что даёт подавление повторов в суточной выдаче.

Отсечка по бюджету состояния не имеет: канал, лежащий месяц, занимает место в
выдаче каждые сутки. По эпизодной постановке это одно событие, и тридцать
выездов к нему — один пойманный отказ, а не тридцать. Посуточная точность
считает их тридцатью попаданиями и выглядит отлично при бесполезной выдаче.

Мерим тем, что чувствует диспетчер: сколько РАЗНЫХ событий поймано на сотню
выданных алертов.
"""
import datetime as dt
import sys

import polars as pl

from mkl import calibrate, metrics, serve, store, train
from mkl.config import HOLDOUT_END, HOLDOUT_START
from mkl.cv import Split
from final_eval import (CAL_END, VAL_END, VAL_START, WINDOW_START,
                        _apply_feature_policy, load_labels)

sys.stdout.reconfigure(encoding="utf-8")


def score_holdout(head: str, cfg: dict):
    lab = load_labels(head, cfg)
    feats = _apply_feature_policy(
        store.read_slice(cfg["feature_set"], WINDOW_START, HOLDOUT_END), cfg)
    tr_end = VAL_START - dt.timedelta(days=cfg["embargo_days"] + 1)
    fit = train.run(head, feats, lab, [Split(WINDOW_START, tr_end, VAL_START, VAL_END)],
                    params=train.params_for(cfg, train.default_backend()), budget_per_day=cfg["budget_per_day"])
    model, names = fit["model"], fit["feature_names"]
    jk = [k for k in ("ch", "obj", "seg", "day")
          if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=jk, how="inner")
    cal = data.filter((pl.col("day") >= VAL_START) & (pl.col("day") <= CAL_END))
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1], cal["y"].to_numpy())
    hold = data.filter((pl.col("day") >= HOLDOUT_START) & (pl.col("day") <= HOLDOUT_END))
    risk = calibrate.apply(iso, model.predict_proba(train._matrix(hold, names))[:, 1])
    ent = next(k for k in ("ch", "obj", "seg") if k in hold.columns)
    return hold.select([ent, "day", "y"]).with_columns(pl.Series("risk", risk)), ent


ARMS = (
    ("бюджет",                 dict(cooldown_days=0,  confirm_of_3=False)),
    ("бюджет + пауза 3 сут",   dict(cooldown_days=3,  confirm_of_3=False)),
    ("бюджет + пауза 7 сут",   dict(cooldown_days=7,  confirm_of_3=False)),
    ("бюджет + подтверждение", dict(cooldown_days=0,  confirm_of_3=True)),
    ("пауза 7 + подтверждение", dict(cooldown_days=7, confirm_of_3=True)),
)


def main() -> None:
    heads = serve.load_heads()
    for head in (sys.argv[1:] or ["C", "A_link", "D"]):
        cfg = heads[head]
        df, ent = score_holdout(head, cfg)
        print(f"\n{head}  {cfg['title']}  (бюджет {cfg['budget_per_day']}/сут)",
              flush=True)
        print(f"{'режим':<26}{'алертов':>9}{'эпизодов':>10}{'поймано':>9}"
              f"{'на 100 алертов':>16}{'полнота эп.':>13}", flush=True)
        for name, kw in ARMS:
            out = serve.alerts_over_time(df, cfg["budget_per_day"], entity=ent,
                                         per_object=bool(cfg.get("budget_per_object")),
                                         **kw)
            m = metrics.episodes_per_100_alerts(
                out[ent].to_numpy(), out["day"].to_numpy(), out["y"].to_numpy(),
                out["alert"].to_numpy(), horizon_days=cfg["horizon_days"])
            print(f"{name:<26}{m['alerts']:>9,}{m['episodes']:>10,}"
                  f"{m['episodes_caught']:>9,}{m['episodes_per_100_alerts']:>16.1f}"
                  f"{m['episode_recall']:>13.3f}", flush=True)


if __name__ == "__main__":
    main()
