"""Does within-day order of numeric events improve tomorrow's fire-alarm proxy?

Experimental features are computed from the deduplicated event parquet, not
added to the production feature store. All timestamps are on or before the
feature day; labels are the following day's monitoring alarm, not confirmed fires.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS, seg_sql

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
KEYS = ["obj", "seg", "day"]


def build_intraday(year: int) -> pl.DataFrame:
    source_path = PATHS.interim / f"events_year={year}.parquet"
    source = source_path.as_posix()
    cache = PATHS.features / f"fire_order_v2_{year}.parquet"
    if not cache.exists() or cache.stat().st_mtime < source_path.stat().st_mtime:
        con = db.connect()
        seg = seg_sql("picket")
        con.execute(f"""
        COPY (
          WITH channel_day AS (
            SELECT obj, {seg} AS seg, day, ch, stype,
                   count(*) AS n,
                   arg_min(val_num, struct_pack(t:=ts, i:=event_id, v:=val_num))
                     AS first_value,
                   arg_max(val_num, struct_pack(t:=ts, i:=event_id, v:=val_num))
                     AS last_value,
                   avg(val_num) FILTER (WHERE hour(ts) < 6) AS early_mean,
                   avg(val_num) FILTER (WHERE hour(ts) >= 18) AS late_mean
            FROM read_parquet('{source}')
            WHERE obj IS NOT NULL
              AND ((stype = 'Датчик температуры' AND val_num BETWEEN -60 AND 150)
                OR (stype = 'Газовый датчик' AND val_num BETWEEN 0 AND 327.67))
            GROUP BY obj, seg, day, ch, stype
          ), delta AS (
            SELECT *,
                   CASE WHEN n >= 2 THEN last_value - first_value END AS d_first_last,
                   late_mean - early_mean AS d_early_late
            FROM channel_day
          )
          SELECT obj, seg, day,
                 count(*) FILTER (WHERE stype = 'Датчик температуры'
                                   AND d_first_last IS NOT NULL)
                   AS order_temp_channels,
                 max(d_first_last) FILTER (WHERE stype = 'Датчик температуры')
                   AS order_temp_delta_max,
                 avg(d_first_last) FILTER (WHERE stype = 'Датчик температуры')
                   AS order_temp_delta_mean,
                 count(*) FILTER (WHERE stype = 'Датчик температуры'
                                   AND d_first_last >= 1) AS order_temp_rise_1,
                 count(*) FILTER (WHERE stype = 'Датчик температуры'
                                   AND d_first_last >= 3) AS order_temp_rise_3,
                 max(d_early_late) FILTER (WHERE stype = 'Датчик температуры')
                   AS order_temp_early_late_max,
                 count(*) FILTER (WHERE stype = 'Датчик температуры'
                                   AND d_early_late IS NOT NULL)
                   AS order_temp_early_late_channels,
                 count(*) FILTER (WHERE stype = 'Газовый датчик'
                                   AND d_first_last IS NOT NULL)
                   AS order_gas_channels,
                 max(d_first_last) FILTER (WHERE stype = 'Газовый датчик')
                   AS order_gas_delta_max,
                 count(*) FILTER (WHERE stype = 'Газовый датчик'
                                   AND d_first_last >= 0.05) AS order_gas_rise_005
          FROM delta GROUP BY obj, seg, day
        ) TO '{cache.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        con.close()
        print(f"built {cache.name}", flush=True)
    return pl.read_parquet(cache)


def evaluate(fit: dict, block: pl.DataFrame, budget: int) -> dict:
    y = block["y"].to_numpy()
    p = fit["model"].predict_proba(
        train._matrix(block, fit["feature_names"]))[:, 1]
    out = metrics.daily_budget_summary(y, p, block["day"].to_numpy(), budget)
    return {"n": len(y), "positives": int(y.sum()),
            "pr_auc": metrics.pr_auc(y, p),
            "daily_precision": out["daily_precision_at_k"],
            "daily_recall": out["daily_recall_at_k"]}


def main() -> None:
    cfg = serve.load_heads()["B"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    con.close()
    base = store.read_slice("segment", START, END)
    base = base.select([c for c in base.columns if not c.startswith("sensor_")])
    intraday = pl.concat([build_intraday(y) for y in (2023, 2024, 2025)])
    extended = base.join(intraday, on=KEYS, how="left")
    onset = base.join(lab, on=KEYS).filter(
        pl.col("days_since_fire").is_null() |
        (pl.col("days_since_fire") >= 7))
    onset_ext = extended.join(lab, on=KEYS).filter(
        pl.col("days_since_fire").is_null() |
        (pl.col("days_since_fire") >= 7))
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"features": [c for c in extended.columns if c.startswith("order_")],
               "folds": []}
    for split in splits:
        date_filter = ((pl.col("day") >= split.test_start) &
                       (pl.col("day") <= split.test_end))
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, feats, quiet in (("daily", base, onset),
                                   ("with_order", extended, onset_ext)):
            fit = train.run("B", feats, lab, [split],
                            params=train.params_for(cfg, "lgbm"),
                            budget_per_day=cfg["budget_per_day"], backend="lgbm")
            if fit["model"] is None:
                raise ValueError(f"{name}: no fitted model")
            row[name] = {"all": {k: fit["folds"][0][k]
                                  for k in ("pr_auc", "daily_precision_at_k",
                                            "daily_recall_at_k")},
                         "onset": evaluate(fit, quiet.filter(date_filter),
                                           cfg["budget_per_day"])}
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    results["mean"] = {
        name: {kind: {metric: float(np.mean([r[name][kind][metric]
                                              for r in results["folds"]]))
                     for metric in (("pr_auc", "daily_precision_at_k",
                                     "daily_recall_at_k") if kind == "all"
                                    else ("pr_auc", "daily_precision",
                                          "daily_recall"))}
               for kind in ("all", "onset")}
        for name in ("daily", "with_order")}
    out = PATHS.reports / "fire_order_experiment.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
