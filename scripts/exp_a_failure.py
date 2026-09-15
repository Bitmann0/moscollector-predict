"""Голова A: отказ датчика. E0 выбирает окно обучения, E1 — вариант метки,
далее лестница бейзлайнов B0…B7, каждая ступень обязана побить предыдущую.
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import cv, db, experiments, labels, metrics, store, train
from mkl.config import EMBARGO_DAYS, HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
BUDGET_PER_DAY = 20


def load_labels(window_start: dt.date, variant: str,
                horizon_days: int = 1, eligible_only: bool = False) -> pl.DataFrame:
    """eligible_only оставляет только каналы, у которых вообще бывают отказы.

    Каналы, ни разу не отказавшие за 7,5 лет, раздувают знаменатель и топят
    базовую ставку, ничего не добавляя к обучению.
    """
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_sensor_failure(con, variant=variant, horizon_days=horizon_days)
    where = "day >= ? AND day <= ?"
    if eligible_only:
        where += (" AND ch IN (SELECT DISTINCT ch FROM episodes "
                  "WHERE dur_s >= 3600 AND NOT is_group AND dur_s <= 2592000 "
                  "AND coalesce(gap_before_s, 0) <= 604800)")
    df = con.execute(
        f"SELECT ch, day, y FROM label_failure WHERE {where}",
        [window_start, TRAIN_END],
    ).pl()
    con.close()
    return df


def load_features(window_start: dt.date, stride: int = 1) -> pl.DataFrame:
    f = store.read_slice("sensor", window_start, TRAIN_END)
    if stride > 1:
        f = f.filter(pl.col("day").dt.ordinal_day() % stride == 0)
    return f


def evaluate(step: str, feats, lab, splits, params=None, cols=None,
             note="", extra=None) -> dict:
    f = feats.select(cols) if cols else feats
    out = train.run("A", f, lab, splits, params=params, budget_per_day=BUDGET_PER_DAY)
    m = out["mean"]
    experiments.log({"head": "A", "step": step, "note": note,
                     "n_features": len(out["feature_names"]), **m, **(extra or {})})
    print(f"  {step:4} {note:32} PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
          f"P@k={m.get('precision_at_k', float('nan')):.3f}  "
          f"R@k={m.get('recall_at_k', float('nan')):.3f}  "
          f"lift={m.get('lift_at_k', float('nan')):.1f}", flush=True)
    return out


def make_splits(lab: pl.DataFrame, n_splits: int = 3) -> list:
    days = sorted(lab["day"].unique().to_list())
    return cv.walk_forward(days, n_splits=n_splits, test_days=30,
                           embargo_days=EMBARGO_DAYS)


def main() -> None:
    print("=== E0: окно обучения ===", flush=True)
    e0 = {}
    for name, start in [("2019+", dt.date(2019, 1, 1)), ("2023+", dt.date(2023, 1, 1))]:
        lab = load_labels(start, "L3")
        feats = load_features(start, stride=3)
        out = evaluate("E0", feats, lab, make_splits(lab, 2), note=f"окно {name}",
                       params={"n_estimators": 200}, extra={"window": name})
        e0[name] = out["mean"].get("pr_auc", float("nan"))
        del feats, lab
    window_start = dt.date(2019, 1, 1) if e0["2019+"] > e0["2023+"] else dt.date(2023, 1, 1)
    print(f"  -> выбрано окно: {window_start}\n", flush=True)

    print("=== E1: вариант метки ===", flush=True)
    print("Сравнение по lift, а не по PR-AUC: у меток разная базовая ставка,", flush=True)
    print("и PR-AUC между ними несравним напрямую.", flush=True)
    e1 = {}
    feats_s = load_features(window_start, stride=3)
    for variant in ("L1", "L2", "L3", "L4", "L5", "L6"):
        lab = load_labels(window_start, variant)
        if lab["y"].sum() == 0:
            print(f"  {variant}: позитивов нет, пропуск", flush=True)
            continue
        out = evaluate("E1", feats_s, lab, make_splits(lab, 2), note=f"метка {variant}",
                       params={"n_estimators": 200}, extra={"variant": variant})
        e1[variant] = out["mean"].get("lift_at_k", float("nan"))

    # L1 — это дребезг, а не отказ: p90 длительности 1,8 минуты. Держим его как
    # референс «деградации», но голова A целится в устойчивый отказ, ради
    # которого выезжает бригада. L4 — чистое молчание, тоже референс.
    SUSTAINED = ("L2", "L3", "L5", "L6")
    pool = {k: v for k, v in e1.items() if k in SUSTAINED}
    variant = max(pool, key=lambda k: (pool[k] if pool[k] == pool[k] else -1))
    print(f"  -> выбрана метка: {variant} (референс-метки L1/L4 в журнале)\n", flush=True)

    print("=== E2: горизонт и состав популяции ===", flush=True)
    print("ТЗ требует горизонт НЕ МЕНЕЕ 24 ч, поэтому 3 и 7 суток допустимы.", flush=True)
    e2 = {}
    for horizon in (1, 3, 7):
        for eligible in (False, True):
            lab_h = load_labels(window_start, variant, horizon, eligible)
            if lab_h.is_empty() or lab_h["y"].sum() == 0:
                continue
            tag = f"{horizon} сут, {'только отказывавшие' if eligible else 'все каналы'}"
            out = evaluate("E2", feats_s, lab_h, make_splits(lab_h, 2), note=tag,
                           params={"n_estimators": 200},
                           extra={"horizon_days": horizon, "eligible_only": eligible})
            e2[(horizon, eligible)] = out["mean"].get("lift_at_k", float("nan"))
    horizon, eligible = max(e2, key=lambda k: (e2[k] if e2[k] == e2[k] else -1))
    print(f"  -> выбран горизонт {horizon} сут, "
          f"популяция: {'только отказывавшие' if eligible else 'все каналы'}\n", flush=True)
    del feats_s

    lab = load_labels(window_start, variant, horizon, eligible)
    feats = load_features(window_start)
    splits = make_splits(lab, 3)
    print(f"строк фич: {feats.height:,}  меток: {lab.height:,}  "
          f"позитивов: {lab['y'].sum():,} ({lab['y'].mean():.4%})\n", flush=True)

    joined = feats.join(lab, on=["ch", "day"], how="inner")
    y = joined["y"].to_numpy()
    n_days = sum((s.test_end - s.test_start).days + 1 for s in splits) // len(splits)
    budget = BUDGET_PER_DAY * n_days

    print("=== лестница бейзлайнов ===", flush=True)
    rng = np.random.default_rng(42)
    b0 = metrics.summary(y, rng.random(len(y)), budget=budget)
    experiments.log({"head": "A", "step": "B0", "note": "случайный", **b0})
    print(f"  B0   {'случайный':32} PR-AUC={b0['pr_auc']:.4f}  "
          f"P@k={b0['precision_at_k']:.3f}  R@k={b0['recall_at_k']:.3f}", flush=True)

    b1_score = joined["n_alarms_w7"].fill_null(0).cast(pl.Float64).to_numpy()
    b1 = metrics.summary(y, b1_score, budget=budget)
    experiments.log({"head": "A", "step": "B1",
                     "note": "правило ОДС: тревоги за 7 сут", **b1})
    print(f"  B1   {'правило ОДС (тревоги за 7 сут)':32} PR-AUC={b1['pr_auc']:.4f}  "
          f"P@k={b1['precision_at_k']:.3f}  R@k={b1['recall_at_k']:.3f}  "
          f"lift={b1['lift_at_k']:.1f}", flush=True)
    del joined

    keys = ["ch", "day"]
    simple = keys + [c for c in ["n_alarms_w7", "n_alarms_w30", "n_bad_w7", "n_bad_w30",
                                 "n_chatter_1min_w7", "days_since_last_alarm",
                                 "days_since_last_bad", "silence_z", "n_events_w7",
                                 "age_days"] if c in feats.columns]
    evaluate("B2", feats, lab, splits, cols=simple,
             params={"n_estimators": 200}, note="10 ручных фич")

    peer_cols = [c for c in feats.columns if c.startswith("peer_")]
    spat_cols = [c for c in feats.columns
                 if c.startswith("nbr_") or c == "val_minus_seg_mean"]
    base_cols = [c for c in feats.columns if c not in peer_cols + spat_cols]

    evaluate("B3", feats, lab, splits, cols=base_cols, note="LightGBM без peer/spatial")
    evaluate("B4", feats, lab, splits, cols=base_cols + peer_cols, note="+ peer-relative")
    best = evaluate("B5", feats, lab, splits, note="+ пространственные")
    evaluate("B7", feats, lab, splits,
             params={"n_estimators": 1200, "learning_rate": 0.02, "num_leaves": 127},
             note="долгое обучение")

    print("\n=== топ-20 признаков по gain (B5) ===", flush=True)
    print(train.importance(best["model"], best["feature_names"], top=20).to_pandas()
          .to_string(index=False))

    print("\n=== журнал головы A ===", flush=True)
    print(experiments.table("A").to_pandas().to_string(index=False))

    with open(PATHS.reports / "head_a_choice.txt", "w", encoding="utf-8") as f:
        f.write(f"window_start={window_start}\nvariant={variant}\n"
                f"horizon_days={horizon}\neligible_only={eligible}\n")


if __name__ == "__main__":
    main()
