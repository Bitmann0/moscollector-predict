"""Финальная оценка на отложенном периоде 2026-01-01 … 2026-06-30.

Отложенный период используется здесь впервые. Правки моделей после взгляда на
него запрещены: это единственный честный замер.
"""
import datetime as dt
import sys
import time

import polars as pl

from mkl import calibrate, db, experiments, labels, metrics, serve, store, train
from mkl.config import HOLDOUT_END, HOLDOUT_START, PATHS
from mkl.cv import Split

sys.stdout.reconfigure(encoding="utf-8")

WINDOW_START = dt.date(2023, 1, 1)
VAL_START = dt.date(2025, 10, 1)
VAL_END = dt.date(2025, 12, 31)

def _choice() -> dict:
    """Конфигурация головы A, выбранная экспериментами E0–E2."""
    out = {"variant": "L6", "horizon_days": "1", "eligible_only": "False"}
    path = PATHS.reports / "head_a_choice.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


BUILDERS = {
    "A": lambda con, h: labels.build_sensor_failure(
        con, variant=_choice()["variant"], horizon_days=h),
    "A_strict": labels.build_sensor_failure_strict,
    "A_deg": labels.build_sensor_degradation,
    "A_prime": labels.build_group_outage,
    "B": labels.build_fire,
    "C": labels.build_intrusion,
    "D": labels.build_wear,
}


def load_labels(head: str, cfg: dict) -> pl.DataFrame:
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    builder = BUILDERS[head]
    if head == "A":
        builder(con, cfg["horizon_days"])
    else:
        builder(con, horizon_days=cfg["horizon_days"])
    df = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ?", [WINDOW_START]
    ).pl()
    con.close()
    return df


def evaluate_head(head: str, cfg: dict) -> dict | None:
    lab = load_labels(head, cfg)
    feats = store.read_slice(cfg["feature_set"], WINDOW_START, HOLDOUT_END)
    train_end = VAL_START - dt.timedelta(days=cfg["embargo_days"] + 1)

    fit = train.run(head, feats, lab,
                    [Split(WINDOW_START, train_end, VAL_START, VAL_END)],
                    budget_per_day=cfg["budget_per_day"])
    if fit["model"] is None:
        print(f"{head:8} обучить не удалось — пропуск", flush=True)
        return None

    model, names = fit["model"], fit["feature_names"]
    join_keys = [k for k in ("ch", "obj", "seg", "day")
                 if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=join_keys, how="inner")

    val = data.filter((pl.col("day") >= VAL_START) & (pl.col("day") <= VAL_END))
    hold = data.filter((pl.col("day") >= HOLDOUT_START) & (pl.col("day") <= HOLDOUT_END))
    if hold.is_empty() or hold["y"].sum() == 0:
        print(f"{head:8} на отложенном периоде нет позитивов — пропуск", flush=True)
        return None

    p_val = model.predict_proba(train._matrix(val, names))[:, 1]
    iso = calibrate.fit_isotonic(p_val, val["y"].to_numpy())
    p_hold = calibrate.apply(iso, model.predict_proba(train._matrix(hold, names))[:, 1])

    n_days = (HOLDOUT_END - HOLDOUT_START).days + 1
    yh = hold["y"].to_numpy()
    res = metrics.summary(yh, p_hold, budget=cfg["budget_per_day"] * n_days)
    op = metrics.target_operating_point(yh, p_hold, min_precision=0.7)
    curve = metrics.budget_curve(yh, p_hold, (n_days, 5 * n_days, 20 * n_days))
    experiments.log({"head": head, "step": "FINAL", "note": "отложенный 2026H1",
                     **res, "curve": curve})
    serve.save(head, model, iso, names)

    ok = "ДА" if op.get("feasible") and op.get("recall", 0) > 0.5 else "нет"
    print(f"{head:8} PR-AUC={res['pr_auc']:.4f}  "
          f"maxP={res['op_precision']:.3f}@R={res['op_recall']:.3f}  "
          f"P@k={res['precision_at_k']:.3f}  lift={res['lift_at_k']:.1f}  "
          f"позитивов={res['n_pos']:,}  цель_ТЗ={ok}", flush=True)
    for c in curve:
        print(f"           бюджет {c['budget']:>6} алертов: P={c['precision']:.3f}  "
              f"R={c['recall']:.3f}  lift={c['lift']:.1f}", flush=True)
    return {"head": head, "title": cfg["title"], "meets_target": ok == "ДА", **res}


def main() -> None:
    heads = serve.load_heads()
    rows = []
    for head, cfg in heads.items():
        r = evaluate_head(head, cfg)
        if r:
            rows.append(r)

    t0 = time.time()
    scored = serve.score_all()
    elapsed = time.time() - t0
    print(f"\nвремя скоринга всех голов: {elapsed:.1f} с (норматив < 300 с)")
    for head, df in scored.items():
        print(f"  {head:8} сущностей={df.height:>6,}  алертов={df['alert'].sum():>4}")

    if rows:
        out = pl.DataFrame(rows).drop("op_feasible", strict=False)
        out.write_csv(PATHS.reports / "final_metrics.csv")
        print("\n" + out.select([
            "head", "title", "n", "n_pos", "base_rate", "pr_auc",
            "op_precision", "op_recall", "precision_at_k", "lift_at_k",
            "meets_target",
        ]).to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
