"""A_link: даёт ли ансамбль трёх бустингов больше попаданий в тех же 20 местах.

Протокол тот же, что в eval_a_link_policy.py: пять последовательных 90-дневных
тестов, для каждого — обучение на прошлом, изотоническая калибровка на
отдельном 30-дневном окне, порог на следующих 30 днях, лимит 20 в сутки и
пауза 7 дней без добора. Unknown остаются в ранжировании и занимают места.

Сравниваются LightGBM (CPU), XGBoost и CatBoost (оба на CUDA) по отдельности,
среднее калиброванных вероятностей LightGBM+XGBoost и среднее всех трёх.
Ёмкость моделей — из heads.yaml (params, params_xgb, params_cat), то есть
сравнение идёт при равной ёмкости, а не «большая модель против малой».

Критерий принятия задан до запуска: ансамбль идёт в продукт, только если на
политике 0.70 он даёт больше попаданий при нижней границе precision не ниже,
чем у LightGBM, суммарно и не хуже чем в 4 из 5 окон.

Запуск (из ml/): python scripts/exp_a_link_ensemble.py --end-date 2026-06-29
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import numpy as np
import polars as pl

from mkl import calibrate, cv, db, labels, serve, store, train
try:
    from scripts.eval_a_link_policy import (
        COOLDOWN_DAYS, N_SPLITS, TEST_DAYS, WINDOW_DAYS, _select,
        _summary_unknown, _threshold)
    from scripts.eval_d_live_policy import _period
except ModuleNotFoundError:  # python scripts/exp_a_link_ensemble.py
    from eval_a_link_policy import (
        COOLDOWN_DAYS, N_SPLITS, TEST_DAYS, WINDOW_DAYS, _select,
        _summary_unknown, _threshold)
    from eval_d_live_policy import _period

sys.stdout.reconfigure(encoding="utf-8")

BACKENDS = ("lgbm", "xgb", "cat")
BLENDS = {"lgbm+xgb": ("lgbm", "xgb"), "lgbm+xgb+cat": BACKENDS}
POLICIES = (0.70, 0.50)


def _frame(block: pl.DataFrame, risk: np.ndarray) -> pl.DataFrame:
    return block.select("ch", "obj", "day", "y").with_columns(pl.Series("risk", risk))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", type=dt.date.fromisoformat,
                        help="last mature label day; default latest available")
    parser.add_argument("--splits", type=int, default=N_SPLITS)
    args = parser.parse_args()
    cfg = serve.load_heads()["A_link"]
    start = dt.date(2023, 1, 1)
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    lab = con.execute(
        "SELECT * FROM label_link WHERE day >= ? AND day <= ?",
        [start, args.end_date or dt.date.max]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], start, lab["day"].max())
    drop = cfg.get("drop_feature_prefixes") or []
    feats = feats.select([c for c in feats.columns
                          if not any(c.startswith(p) for p in drop)])
    keys = [k for k in ("ch", "obj", "day") if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=keys, how="left").sort(["day", "obj", "ch"])
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()),
                             N_SPLITS, TEST_DAYS, cfg["embargo_days"])[-args.splits:]
    rows = []
    for split in splits:
        threshold_end = split.test_start - dt.timedelta(days=cfg["horizon_days"] + 1)
        threshold_start = threshold_end - dt.timedelta(days=WINDOW_DAYS - 1)
        calibration_end = threshold_start - dt.timedelta(days=1)
        calibration_start = calibration_end - dt.timedelta(days=WINDOW_DAYS - 1)
        training_end = calibration_start - dt.timedelta(days=cfg["embargo_days"] + 1)
        cal = _period(data, calibration_start, calibration_end).filter(
            pl.col("y").is_not_null())
        thr = _period(data, threshold_start, threshold_end)
        test = _period(data, split.test_start, split.test_end)
        risks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        seconds = {}
        for backend in BACKENDS:
            t0 = time.perf_counter()
            fit = train.run(
                "A_link", feats, lab,
                [cv.Split(start, training_end, calibration_start, calibration_end)],
                params=train.params_for(cfg, backend), backend=backend,
                budget_per_day=cfg["budget_per_day"],
                horizon_days=cfg["horizon_days"])
            model, names = fit["model"], fit["feature_names"]
            iso = calibrate.fit_isotonic(
                model.predict_proba(train._matrix(cal, names))[:, 1],
                cal["y"].to_numpy())
            risks[backend] = tuple(
                calibrate.apply(iso, model.predict_proba(train._matrix(b, names))[:, 1])
                for b in (thr, test))
            seconds[backend] = round(time.perf_counter() - t0, 1)
            del fit, model
        for name, parts in BLENDS.items():
            risks[name] = tuple(np.mean([risks[p][i] for p in parts], axis=0)
                                for i in (0, 1))
        scores = {}
        for name, (r_thr, r_test) in risks.items():
            thr_scores, test_scores = _frame(thr, r_thr), _frame(test, r_test)
            out = {}
            for minimum in POLICIES:
                pick = _select(thr_scores, cfg, minimum)
                out[f"min_precision_{minimum:.2f}"] = {
                    "feasible": bool(pick.get("feasible")),
                    "threshold": float(_threshold(pick, thr_scores)),
                    **_summary_unknown(test_scores, _threshold(pick, thr_scores), cfg)}
            out["fixed_top_20"] = _summary_unknown(test_scores, None, cfg)
            scores[name] = out
        row = {"test_start": str(split.test_start), "test_end": str(split.test_end),
               "training_end": str(training_end), "fit_seconds": seconds,
               "scores": scores}
        rows.append(row)
        print(json.dumps({
            "test_start": row["test_start"], "fit_seconds": seconds,
            **{name: {p: (v["alerts"], v["hits"], round(v["precision_lower_bound"] or 0, 4))
                      for p, v in s.items()} for name, s in scores.items()}},
            ensure_ascii=False), flush=True)
    pooled = {}
    for name in rows[0]["scores"]:
        pooled[name] = {}
        for policy in rows[0]["scores"][name]:
            alerts = sum(r["scores"][name][policy]["alerts"] for r in rows)
            hits = sum(r["scores"][name][policy]["hits"] for r in rows)
            pooled[name][policy] = {
                "alerts": alerts, "hits": hits,
                "precision_lower_bound": round(hits / alerts, 4) if alerts else None}
    result = {"head": "A_link", "label_variant": cfg["variant"],
              "evaluated_through": str(lab["day"].max()),
              "protocol": "eval_a_link_policy.py; cooldown "
                          f"{COOLDOWN_DAYS}d; budget {cfg['budget_per_day']}/day",
              "pooled": pooled, "folds": rows}
    path = store.PATHS.reports / "a_link_ensemble_experiment.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")
    print(json.dumps(pooled, ensure_ascii=False, indent=2))
    print(f"saved {path}", flush=True)


if __name__ == "__main__":
    main()
