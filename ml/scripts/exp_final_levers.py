"""Последний цикл гипотез: восстановление A_strict, стекинг и вес по свежести.

Расширение фичестора со 117 до 165 признаков улучшило шесть голов из восьми,
но голову аномалии датчика просадило: при базовой ставке 1,2% лишние полсотни
признаков разбавляют сигнал. Здесь проверяются три способа это вернуть и два
рычага, которые работают поверх любых признаков.
"""
import datetime as dt
import sys

import polars as pl

from mkl import config, cv, db, experiments, labels, serve, stacking, store, train
from mkl.config import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
TEST_DAYS = 90

BUILDERS = {
    "A": lambda con, h: labels.build_sensor_failure(
        con, variant=config.head_a_choice()["variant"], horizon_days=h),
    "A_strict": labels.build_sensor_failure_strict,
    "A_deg": labels.build_sensor_degradation,
    "A_prime": labels.build_group_outage,
    "B": labels.build_fire,
    "C": labels.build_intrusion,
    "D": labels.build_wear,
    "E": labels.build_flood,
}

WINDOW_START = dt.date.fromisoformat(config.head_a_choice()["window_start"])


def load(head: str, cfg: dict):
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    b = BUILDERS[head]
    b(con, cfg["horizon_days"]) if head == "A" else b(con, horizon_days=cfg["horizon_days"])
    lab = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ? AND day <= ?",
        [WINDOW_START, TRAIN_END]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], WINDOW_START, TRAIN_END)
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(p) for p in drop)])
    return feats, lab


def splits_for(lab: pl.DataFrame, cfg: dict):
    return cv.walk_forward(sorted(lab["day"].unique().to_list()),
                           n_splits=3, test_days=TEST_DAYS,
                           embargo_days=cfg["embargo_days"])


def report(head: str, step: str, note: str, out: dict) -> float:
    m = out["mean"]
    experiments.log({"head": head, "step": step, "note": note,
                     "n_features": len(out["feature_names"]), **m})
    norm = m.get("pr_auc_norm", float("nan"))
    print(f"  {note:34} норм={norm:.4f}  "
          f"P@R50={m.get('p_at_r50', float('nan')):.3f}  "
          f"maxP={m.get('op_precision', float('nan')):.3f}@R="
          f"{m.get('op_recall', float('nan')):.3f}  "
          f"эпизод_R={m.get('episode_recall', float('nan')):.3f}", flush=True)
    return norm if norm == norm else -1.0


def recover_a_strict(heads: dict) -> None:
    """Три способа вернуть голову аномалии датчика."""
    cfg = heads["A_strict"]
    feats, lab = load("A_strict", cfg)
    sp = splits_for(lab, cfg)
    print(f"\n=== A_strict: восстановление ({feats.width} признаков, "
          f"{lab['y'].sum():,} позитивов) ===", flush=True)

    tele = [c for c in feats.columns
            if c.startswith("val_") or c in ("max_flat_run", "n_distinct_vals",
                                             "n_val_ok", "n_val_bad", "n_saturated",
                                             "n_val_nonzero", "n_val_gt005",
                                             "n_val_gt02", "flat_run_ratio",
                                             "distinct_ratio", "sampling_ratio",
                                             "nonzero_frac", "nonzero_ratio",
                                             "bad_value_frac")]
    variants = [
        ("полный набор", feats, None),
        ("без телеметрии", feats.select([c for c in feats.columns if c not in tele]), None),
        ("сильная регуляризация", feats,
         {"colsample_bytree": 0.4, "min_child_samples": 500, "reg_lambda": 10.0}),
        ("мелкие деревья", feats,
         {"num_leaves": 31, "min_child_samples": 300, "n_estimators": 600}),
    ]
    best, best_note = -1.0, None
    for note, f, params in variants:
        out = train.run("A_strict", f, lab, sp, params=params,
                        budget_per_day=cfg["budget_per_day"])
        v = report("A_strict", "B10", note, out)
        if v > best:
            best, best_note = v, note
    print(f"  -> лучший вариант: {best_note} ({best:.4f}), "
          f"прежнее значение до расширения признаков 0.3059", flush=True)


def stacking_experiment(heads: dict) -> None:
    """Приор плотной головы как признак для редких."""
    print("\n=== Стекинг: приор головы A в редкие головы ===", flush=True)
    cfg_a = heads["A"]
    feats_a, lab_a = load("A", cfg_a)
    sp_a = splits_for(lab_a, cfg_a)
    prior = stacking.oof_predictions("A", feats_a, lab_a, sp_a,
                                     params={"n_estimators": 300}, entity="ch")
    del feats_a, lab_a
    print(f"  приор посчитан на {prior.height:,} канало-суток", flush=True)

    for head in ("A_strict", "A_deg", "D"):
        cfg = heads[head]
        feats, lab = load(head, cfg)
        sp = splits_for(lab, cfg)
        base = train.run(head, feats, lab, sp, params=cfg.get("params"),
                         budget_per_day=cfg["budget_per_day"])
        b = report(head, "B11", f"{head} без приора", base)
        f2 = stacking.attach_prior(feats, prior, on=["ch", "day"])
        out = train.run(head, f2, lab, sp, params=cfg.get("params"),
                        budget_per_day=cfg["budget_per_day"])
        v = report(head, "B11", f"{head} с приором A", out)
        print(f"  -> {head}: {'приор помогает' if v > b else 'приор не помогает'}"
              f" ({v - b:+.4f})", flush=True)
        del feats, lab, f2


def recency_experiment(heads: dict) -> None:
    """Экспоненциальный вес по свежести."""
    print("\n=== Вес по свежести ===", flush=True)
    for head in ("A", "D"):
        cfg = heads[head]
        feats, lab = load(head, cfg)
        sp = splits_for(lab, cfg)
        base = train.run(head, feats, lab, sp, params=cfg.get("params"),
                         budget_per_day=cfg["budget_per_day"])
        b = report(head, "B12", f"{head} без веса", base)
        for hl in (180.0, 540.0):
            out = train.run(head, feats, lab, sp, params=cfg.get("params"),
                            budget_per_day=cfg["budget_per_day"],
                            half_life_days=hl)
            v = report(head, "B12", f"{head} полупериод {hl:.0f} сут", out)
            print(f"  -> {head} hl={hl:.0f}: {v - b:+.4f}", flush=True)
        del feats, lab


def main() -> None:
    heads = serve.load_heads()
    recover_a_strict(heads)
    stacking_experiment(heads)
    recency_experiment(heads)
    print("\n=== журнал ===", flush=True)
    print(experiments.table().to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
