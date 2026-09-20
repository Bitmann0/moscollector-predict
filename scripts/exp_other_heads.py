"""Головы, кроме основной A: аномалия и деградация датчика, массовый отказ
объекта, пожарный риск участка, несанкционированный доступ, износ агрегатов
и риск подтопления.
"""
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import cv, db, experiments, labels, metrics, serve, store, train
from mkl.config import HOLDOUT_START, PATHS

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)


def _window_start() -> dt.date:
    """Окно обучения берётся из выбора эксперимента E0, а не задаётся заново."""
    path = PATHS.reports / "head_a_choice.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("window_start="):
                return dt.date.fromisoformat(line.split("=", 1)[1].strip())
    return dt.date(2023, 1, 1)


WINDOW_START = _window_start()

BUILDERS = {
    "A_strict": labels.build_sensor_failure_strict,
    "A_deg": labels.build_sensor_degradation,
    "A_prime": labels.build_group_outage,
    "B": labels.build_fire,
    "C": labels.build_intrusion,
    "D": labels.build_wear,
    "E": labels.build_flood,
}


def load_labels(head: str, cfg: dict) -> pl.DataFrame:
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    BUILDERS[head](con, horizon_days=cfg["horizon_days"])
    df = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ? AND day <= ?",
        [WINDOW_START, TRAIN_END],
    ).pl()
    con.close()
    return df


def run_head(head: str, cfg: dict) -> None:
    lab = load_labels(head, cfg)
    if lab.is_empty() or lab["y"].sum() == 0:
        print(f"{head:8} позитивов нет — пропуск", flush=True)
        return

    feats = store.read_slice(cfg["feature_set"], WINDOW_START, TRAIN_END)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=30,
                             embargo_days=cfg["embargo_days"])

    join_keys = [k for k in ("ch", "obj", "seg", "day")
                 if k in feats.columns and k in lab.columns]
    joined = feats.join(lab, on=join_keys, how="inner")

    # Бейзлайн меряется ПОФОЛДОВО, ровно как модель в train.run, и результаты
    # усредняются по фолдам. Прежде он считался на объединении трёх тестовых
    # окон (90 суток), а бюджет получал как на одно (30) — то есть втрое
    # меньше алертов, чем у модели на тех же строках. Весь наблюдавшийся отрыв
    # модели по полноте объяснялся этим, а по точности бейзлайн уже обходил её.
    rule_col = next((c for c in ("n_alarms_w7", "n_alarms") if c in joined.columns), None)
    if rule_col:
        days_col = joined["day"].to_numpy()
        score = joined[rule_col].fill_null(0).cast(pl.Float64).to_numpy()
        yy = joined["y"].to_numpy()
        per_fold = []
        for s in splits:
            m = ((days_col >= np.datetime64(s.test_start))
                 & (days_col <= np.datetime64(s.test_end)))
            if not m.any() or yy[m].sum() == 0:
                continue
            nd = (s.test_end - s.test_start).days + 1
            per_fold.append(metrics.summary(
                yy[m], score[m], budget=cfg["budget_per_day"] * nd))
        if per_fold:
            b1 = {k: float(np.nanmean([f[k] for f in per_fold]))
                  for k in per_fold[0] if isinstance(per_fold[0][k], (int, float))}
            b1["n_pos"] = int(sum(f["n_pos"] for f in per_fold))
            experiments.log({"head": head, "step": "B1",
                             "note": f"правило ОДС: {rule_col}, пофолдово", **b1})
            print(f"{head:8} B1 {'правило ОДС':26} база={b1['base_rate']:.4f}  "
                  f"PR-AUC={b1['pr_auc']:.4f}  норм={b1['pr_auc_norm']:.4f}  "
                  f"lift={b1['lift_at_k']:.1f}  P@R50={b1['p_at_r50']:.3f}", flush=True)
    del joined

    out = train.run(head, feats, lab, splits, budget_per_day=cfg["budget_per_day"])
    m = out["mean"]
    experiments.log({"head": head, "step": "B5", "note": cfg["title"],
                     "n_features": len(out["feature_names"]), **m})
    br = m.get("base_rate", float("nan"))
    print(f"{head:8} B5 {cfg['title']:26} база={br:.4f}  "
          f"PR-AUC={m.get('pr_auc', float('nan')):.4f}  "
          f"норм={m.get('pr_auc_norm', float('nan')):.4f}  "
          f"lift={m.get('lift_at_k', float('nan')):.1f}  "
          f"P@R50={m.get('p_at_r50', float('nan')):.3f}  "
          f"maxP={m.get('op_precision', float('nan')):.3f}@R="
          f"{m.get('op_recall', float('nan')):.3f}  "
          f"позитивов={m.get('n_pos', 0):,}", flush=True)
    if br == br and br > 0.25:
        print(f"   ВНИМАНИЕ: позитивов больше четверти — при такой базовой "
              f"ставке порог Precision 0.7 берётся почти даром, "
              f"судить нужно по нормированному PR-AUC", flush=True)

    if out["model"] is not None:
        print("   топ-10 признаков: " + ", ".join(
            train.importance(out["model"], out["feature_names"], top=10)["feature"]
        ), flush=True)


def main() -> None:
    heads = serve.load_heads()
    for head in ("A_strict", "A_deg", "A_prime", "B", "C", "D", "E"):
        print(flush=True)
        run_head(head, heads[head])

    print("\n=== сводка по всем головам ===", flush=True)
    print(experiments.table().to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
