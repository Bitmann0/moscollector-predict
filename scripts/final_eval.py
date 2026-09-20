"""Оценка на отложенном периоде 2026-01-01 … 2026-06-30.

Прежняя версия этого докстринга утверждала, что период используется впервые.
Журнал показывает обратное: прогонов было много и на разных состояниях кода.
Запечатанный период тем и ценен, что его смотрят один раз, поэтому приведённые
здесь числа следует считать валидационными. Счётчик просмотров печатается при
запуске и пишется в reports/holdout_uses.md — см. scripts/holdout_uses.py.

Рабочая точка выбирается на валидации (декабрь 2025) и приходит сюда готовым
числом. Оракульная точка, подобранная по ответам самого отложенного периода,
считается рядом, но только как верхняя граница: разница между ней и честной —
величина, на которую отчёт завышался бы.
"""
import datetime as dt
import sys
import time

import polars as pl

from mkl import config, calibrate, db, experiments, labels, metrics, serve, store, train
from mkl.config import HOLDOUT_END, HOLDOUT_START, PATHS
from mkl.cv import Split

sys.stdout.reconfigure(encoding="utf-8")

VAL_START = dt.date(2025, 10, 1)
VAL_END = dt.date(2025, 12, 31)
# Валидация делится надвое: на первой половине учится изотоника, на второй
# выбирается рабочая точка. Делать и то и другое на одном куске нельзя —
# калиброванные скоры на нём внутривыборочные, и порог сядет на них.
CAL_END = dt.date(2025, 11, 30)
THR_START = dt.date(2025, 12, 1)

def _choice() -> dict:
    return config.head_a_choice()


# Окно обучения берётся из выбора эксперимента E0, а не задаётся заново.
WINDOW_START = dt.date.fromisoformat(_choice().get("window_start", "2023-01-01"))



def load_labels(head: str, cfg: dict) -> pl.DataFrame:
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    df = con.execute(
        f"SELECT * FROM {cfg['label']} WHERE day >= ?", [WINDOW_START]
    ).pl()
    con.close()
    return df


def _apply_feature_policy(feats: pl.DataFrame, cfg: dict) -> pl.DataFrame:
    """Убрать семейства признаков, отклонённые лестницей для этой головы."""
    drop = cfg.get("drop_feature_prefixes") or []
    if not drop:
        return feats
    keep = [c for c in feats.columns if not any(c.startswith(p) for p in drop)]
    return feats.select(keep)


def evaluate_head(head: str, cfg: dict) -> dict | None:
    lab = load_labels(head, cfg)
    feats = _apply_feature_policy(
        store.read_slice(cfg["feature_set"], WINDOW_START, HOLDOUT_END), cfg)
    train_end = VAL_START - dt.timedelta(days=cfg["embargo_days"] + 1)

    fit = train.run(head, feats, lab,
                    [Split(WINDOW_START, train_end, VAL_START, VAL_END)],
                    params=cfg.get("params"),
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

    cal = val.filter(pl.col("day") <= CAL_END)
    thr_set = val.filter(pl.col("day") >= THR_START)
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1], cal["y"].to_numpy())

    # Рабочая точка выбирается ЗДЕСЬ, на декабре, и на отложенный период
    # приходит готовым числом.
    p_thr = calibrate.apply(iso, model.predict_proba(train._matrix(thr_set, names))[:, 1])
    pick = metrics.target_operating_point(p=p_thr, y=thr_set["y"].to_numpy(),
                                          min_precision=0.7)
    thr = pick.get("threshold") if pick.get("feasible") else 1.0

    p_hold = calibrate.apply(iso, model.predict_proba(train._matrix(hold, names))[:, 1])
    n_days = (HOLDOUT_END - HOLDOUT_START).days + 1
    yh = hold["y"].to_numpy()
    res = metrics.summary(yh, p_hold, budget=cfg["budget_per_day"] * n_days)
    honest = metrics.at_threshold(yh, p_hold, thr)
    verdict = metrics.meets_target(honest, n_days, cfg["budget_per_day"])
    # Оракульная точка остаётся, но как верхняя граница, а не как результат:
    # разница между ней и честной — величина, на которую отчёт завышался.
    oracle = metrics.target_operating_point(yh, p_hold, min_precision=0.7)
    curve = metrics.budget_curve(yh, p_hold, (n_days, 5 * n_days, 20 * n_days))
    res = {**res,
           "thr_precision": honest["precision"], "thr_recall": honest["recall"],
           "thr_k": honest["k"], "threshold_from_val": thr,
           "oracle_precision": oracle.get("precision"),
           "oracle_recall": oracle.get("recall"), "oracle_k": oracle.get("k"),
           **verdict}
    experiments.log({"head": head, "step": "FINAL", "note": "отложенный 2026H1",
                     **res, "curve": curve})
    serve.save(head, model, iso, names, threshold=thr)

    ok = "ДА" if verdict["meets_target"] else "нет"
    print(f"{head:8} PR-AUC={res['pr_auc']:.4f}  "
          f"честно P={honest['precision']:.3f}@R={honest['recall']:.3f} "
          f"({verdict['alerts_per_day']:.0f} алертов/сут при "
          f"{cfg['budget_per_day']})  "
          f"оракул P={oracle.get('precision', float('nan')):.3f}"
          f"@R={oracle.get('recall', float('nan')):.3f}  "
          f"позитивов={res['n_pos']:,}  цель_ТЗ={ok}", flush=True)
    for c in curve:
        print(f"           бюджет {c['budget']:>6} алертов: P={c['precision']:.3f}  "
              f"R={c['recall']:.3f}  lift={c['lift']:.1f}", flush=True)
    return {"head": head, "title": cfg["title"], "meets_target": ok == "ДА", **res}


def _warn_holdout_uses() -> None:
    from holdout_uses import uses
    u = uses()
    if u:
        shas = {x["git_sha"] for x in u}
        print(f"ВНИМАНИЕ: отложенный период уже просматривался {len(u)} раз "
              f"на {len(shas)} состояниях кода. Этот прогон — {len(u) + 1}-й. "
              f"Числа следует подавать как валидационные.\n", flush=True)


def main() -> None:
    _warn_holdout_uses()
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
            "pr_auc_norm", "op_precision", "op_recall", "p_at_r50",
            "lift_at_k", "meets_target",
        ]).to_pandas().to_string(index=False))


if __name__ == "__main__":
    main()
