"""Six-hour, four-times-daily guarded-alarm forecast experiment.

Forecasts are issued at 00/06/12/18 before the events at that hour. Every
available candidate is scored. Future coverage is used only to mark outcomes
as known/unknown for fitting and audit, never to select the dispatch queue.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

from exp_intrusion_label_timing import _in, attach_guard_state, build_guard_timeline
from mkl import db, metrics, serve, store, train
from mkl.config import INTRUSION_STATES, PATHS

START = dt.date(2023, 1, 1)
END = dt.date(2025, 12, 30)
TEST_STARTS = (dt.date(2025, 4, 5), dt.date(2025, 7, 4),
               dt.date(2025, 10, 2))
MODEL_NAMES = ("rule_week", "rule_intrusion_24h", "lgb_simple", "lgb_extended",
               "lgb_recorded", "lgb_joint")

ROLL_SQL = """
  CREATE TEMP TABLE roll AS
  SELECT obj, hour AS asof,
    sum(n_events) OVER w6 AS events_6h,
    sum(n_events) OVER w24 AS events_24h,
    sum(n_events) OVER w168 AS events_168h,
    sum(CASE WHEN n_events>0 THEN 1 ELSE 0 END) OVER w24 AS observed_hours_24h,
    sum(CASE WHEN n_events>0 THEN 1 ELSE 0 END) OVER w168 AS observed_hours_168h,
    sum(n_alarms) OVER w24 AS alarms_24h,
    sum(n_intrusion) OVER w6 AS intrusion_6h,
    sum(n_intrusion) OVER w24 AS intrusion_24h,
    sum(n_arm) OVER w24 AS arm_24h,
    sum(n_disarm) OVER w24 AS disarm_24h,
    sum(known_positive) OVER w24 AS known_positive_24h,
    sum(known_positive) OVER w168 AS known_positive_168h,
    sum(unresolved) OVER w168 AS unresolved_168h,
    sum(n_events) OVER future6 AS future_events,
    sum(CASE WHEN n_events>0 THEN 1 ELSE 0 END) OVER future6 AS future_observed_hours,
    sum(known_positive) OVER future6 AS future_known_positive,
    sum(unresolved) OVER future6 AS future_unresolved
  FROM hourly
  WINDOW w6 AS (PARTITION BY obj ORDER BY hour ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING),
         w24 AS (PARTITION BY obj ORDER BY hour ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING),
         w168 AS (PARTITION BY obj ORDER BY hour ROWS BETWEEN 168 PRECEDING AND 1 PRECEDING),
         future6 AS (PARTITION BY obj ORDER BY hour ROWS BETWEEN CURRENT ROW AND 5 FOLLOWING)
  QUALIFY hour(hour) % 6 = 0
"""


def build_snapshot_cache() -> None:
    con = db.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")
    raw = (PATHS.interim / "events_year=*.parquet").as_posix()
    recent = "[" + ", ".join("'" + (PATHS.interim / f"events_year={y}.parquet").as_posix()
                               + "'" for y in (2023, 2024, 2025)) + "]"
    source = f"read_parquet({recent})"
    start = time.perf_counter()
    build_guard_timeline(con, f"read_parquet('{raw}')")
    print("guard timeline", round(time.perf_counter()-start, 1), flush=True)
    con.execute(f"""
      CREATE TEMP TABLE intrusion AS
      SELECT obj, ts, day, val_raw, alarm, stype, ch
      FROM {source}
      WHERE obj IS NOT NULL AND val_raw IN {_in(INTRUSION_STATES)} AND alarm
    """)
    attach_guard_state(con)
    con.execute("""
      CREATE TEMP TABLE alarm_hour AS
      SELECT obj, date_trunc('hour', ts) AS hour,
             count(*) FILTER (WHERE armed_at_event=1) AS known_positive,
             count(*) FILTER (WHERE armed_at_event IS NULL) AS unresolved
      FROM at_time GROUP BY obj, hour
    """)
    print("alarm timeline", round(time.perf_counter()-start, 1), flush=True)
    con.execute(f"""
      CREATE TEMP TABLE event_hour AS
      SELECT obj, date_trunc('hour', ts) AS hour,
             count(*) AS n_events,
             count(*) FILTER (WHERE alarm) AS n_alarms,
             count(*) FILTER (WHERE val_raw IN {_in(INTRUSION_STATES)}) AS n_intrusion,
             count(*) FILTER (WHERE val_raw='На охране') AS n_arm,
             count(*) FILTER (WHERE val_raw='Снято с охраны') AS n_disarm
      FROM {source}
      WHERE obj IS NOT NULL GROUP BY obj, hour
    """)
    print("hour aggregation", round(time.perf_counter()-start, 1), flush=True)
    con.execute("""
      CREATE TEMP TABLE hourly AS
      SELECT objects.obj, series.hour,
             coalesce(e.n_events, 0) AS n_events,
             coalesce(e.n_alarms, 0) AS n_alarms,
             coalesce(e.n_intrusion, 0) AS n_intrusion,
             coalesce(e.n_arm, 0) AS n_arm,
             coalesce(e.n_disarm, 0) AS n_disarm,
             coalesce(a.known_positive, 0) AS known_positive,
             coalesce(a.unresolved, 0) AS unresolved
      FROM (SELECT DISTINCT obj FROM guard) objects
      CROSS JOIN generate_series(TIMESTAMP '2022-12-25 00:00:00',
             TIMESTAMP '2025-12-31 23:00:00', INTERVAL 1 HOUR) series(hour)
      LEFT JOIN event_hour e ON e.obj=objects.obj AND e.hour=series.hour
      LEFT JOIN alarm_hour a ON a.obj=objects.obj AND a.hour=series.hour
    """)
    print("dense hour panel", round(time.perf_counter()-start, 1), flush=True)
    con.execute(ROLL_SQL)
    print("rolling features", round(time.perf_counter()-start, 1), flush=True)
    con.execute("""
      CREATE TEMP TABLE ready AS
      SELECT r.*, g.armed, g.guard_ts,
             date_diff('hour', g.guard_ts, r.asof) AS hours_since_guard
      FROM roll r ASOF LEFT JOIN guard g ON r.obj=g.obj AND r.asof>g.guard_ts
      WHERE r.asof BETWEEN TIMESTAMP '2023-01-02' AND TIMESTAMP '2025-12-30 18:00:00'
    """)
    dst = (PATHS.features / "intrusion_six_hour_snapshots.parquet").as_posix()
    con.execute(f"COPY ready TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    print("snapshots", con.execute("SELECT count(*) FROM ready").fetchone()[0],
          round(time.perf_counter()-start, 1), flush=True)
    con.close()


def load_dataset(min_past_observed_hours: int = 1) -> pl.DataFrame:
    snap = pl.read_parquet(PATHS.features / "intrusion_six_hour_snapshots.parquet")
    snap = snap.filter((pl.col("armed") == 1) &
                       (pl.col("hours_since_guard") <= 168) &
                       (pl.col("observed_hours_24h") >= min_past_observed_hours))
    base = store.read_slice("object", START, END)
    cfg = serve.load_heads()["C"]
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns if
                        not any(c.startswith(prefix) for prefix in drop)])
    base = base.with_columns((pl.col("day")+dt.timedelta(days=1)).alias("asof_day"))
    snap = snap.with_columns(pl.col("asof").dt.date().alias("asof_day"))
    snap = snap.join(base, on=["obj", "asof_day"], how="inner")
    snap = snap.with_columns(
        (pl.col("future_known_positive") > 0).cast(pl.Int8).alias("y"),
        ((pl.col("known_positive_168h") == 0) &
         (pl.col("unresolved_168h") == 0) &
         (pl.col("events_168h") >= 7)).alias("quiet"),
        ((pl.col("future_observed_hours") >= 3) &
         (pl.col("future_unresolved") == 0)).alias("observed_outcome"),
    )
    # Positive event itself establishes observation even if other hours are blank.
    snap = snap.with_columns((pl.col("observed_outcome") | (pl.col("y") == 1)).alias("known"))
    snap = snap.with_columns(pl.col("asof").dt.date().alias("forecast_day"))
    return snap.sort(["asof", "obj"])


def evaluate(frame: pl.DataFrame, scores: np.ndarray, view: str) -> dict:
    if view == "quiet":
        mask = frame["quiet"].to_numpy()
        frame, scores = frame.filter(pl.Series(mask)), scores[mask]
    picked = metrics._daily_top_mask(scores, frame["asof"].to_numpy(), 1)
    known = frame["known"].to_numpy()
    positive = frame["y"].to_numpy().astype(bool)
    alerts = int(picked.sum())
    hits = int((picked & positive).sum())
    unknown_alerts = int((picked & ~known).sum())
    positives = int(positive.sum())
    return {"candidates": len(frame), "known_candidates": int(known.sum()),
            "positives": positives, "alerts": alerts, "hits": hits,
            "unknown_alerts": unknown_alerts,
            "precision_lower_bound": hits/alerts if alerts else None,
            "precision_on_known_alerts": hits/(alerts-unknown_alerts)
            if alerts > unknown_alerts else None,
            "recall_of_recorded_positives": hits/positives if positives else None}


def main(min_past_observed_hours: int = 6, rebuild: bool = False) -> None:
    cache = PATHS.features / "intrusion_six_hour_snapshots.parquet"
    sources = [PATHS.interim / f"events_year={y}.parquet" for y in range(2019, 2026)]
    sources.append(Path(__file__))
    if rebuild or not cache.exists() or any(p.stat().st_mtime > cache.stat().st_mtime
                                              for p in sources):
        build_snapshot_cache()
    data = load_dataset(min_past_observed_hours)
    print("candidates", len(data), "known", int(data["known"].sum()),
          "positives", int(data["y"].sum()), flush=True)
    cfg = serve.load_heads()["C"]
    excluded = {"y", "quiet", "known", "observed_outcome", "future_events",
                "future_observed_hours", "future_known_positive", "future_unresolved",
                "armed", "guard_ts", "hours_since_guard", "forecast_day", "asof_day"}
    base_cols = [c for c in train.feature_columns(data)
                 if c not in excluded and c not in {"events_6h", "events_24h", "events_168h",
                 "alarms_24h", "intrusion_6h", "intrusion_24h", "arm_24h",
                 "disarm_24h", "known_positive_24h", "known_positive_168h",
                 "unresolved_168h", "observed_hours_24h", "observed_hours_168h"}]
    hour_cols = ["events_6h", "events_24h", "events_168h", "alarms_24h",
                 "intrusion_6h", "intrusion_24h", "arm_24h", "disarm_24h",
                 "known_positive_24h", "known_positive_168h", "unresolved_168h",
                 "observed_hours_24h", "observed_hours_168h"]
    # Baseline daily features are from the previous calendar day only.
    simple_cols = base_cols + ["known_positive_168h"]
    extended_cols = base_cols + hour_cols
    results = {"target": "recorded intrusion alarm with armed state at event time in [asof,asof+6h)",
               "cutoffs": "00/06/12/18 local timestamp as recorded in source",
               "candidate": f"guard armed and refreshed <=7d; >={min_past_observed_hours} distinct hours with a raw event in previous 24h; prior-day daily features",
               "minimum_past_observed_hours": min_past_observed_hours,
               "future_coverage": ">=3/6 hours with any raw event, or a known positive; unknown guard alarm makes negatives unresolved",
               "policy": "up to one alert per cutoff, at most four per day",
               "folds": [], "feature_columns": {"simple": simple_cols, "extended": extended_cols}}
    pred_frames = []
    for test_start in TEST_STARTS:
        test_end = test_start + dt.timedelta(days=89)
        cal_end = test_start - dt.timedelta(days=32)
        cal_start = cal_end - dt.timedelta(days=59)
        fit_end = cal_start - dt.timedelta(days=32)
        fit = data.filter((pl.col("forecast_day") <= fit_end) & pl.col("known"))
        fit_all = data.filter(pl.col("forecast_day") <= fit_end)
        cal = data.filter(pl.col("forecast_day").is_between(cal_start, cal_end))
        test = data.filter(pl.col("forecast_day").is_between(test_start, test_end))
        fold = {"fit_end": str(fit_end), "cal_start": str(cal_start),
                "cal_end": str(cal_end), "test_start": str(test_start),
                "test_end": str(test_end), "train_rows": len(fit),
                "train_positives": int(fit["y"].sum()), "variants": {}}
        conditional_scores = None
        for name in MODEL_NAMES:
            if name == "rule_week":
                pc = cal["known_positive_168h"].to_numpy().astype(float)
                pt = test["known_positive_168h"].to_numpy().astype(float)
            elif name == "rule_intrusion_24h":
                pc = cal["intrusion_24h"].to_numpy().astype(float)
                pt = test["intrusion_24h"].to_numpy().astype(float)
            else:
                cols = simple_cols if name == "lgb_simple" else extended_cols
                training = fit_all if name in ("lgb_recorded", "lgb_joint") else fit
                target = "known" if name == "lgb_joint" else "y"
                ytr = training[target].to_numpy()
                model = train._build_model("lgbm", {**cfg["params"], "n_jobs": 4},
                                           (len(ytr)-ytr.sum())/ytr.sum())
                model.fit(train._matrix(training, cols), ytr)
                pc = model.predict_proba(train._matrix(cal, cols))[:, 1]
                pt = model.predict_proba(train._matrix(test, cols))[:, 1]
                if name == "lgb_extended":
                    conditional_scores = (pc.copy(), pt.copy())
                if name == "lgb_joint":
                    # Both components are fitted only on earlier data. Scores
                    # are for ranking; class weighting means no calibration claim.
                    pc *= conditional_scores[0]
                    pt *= conditional_scores[1]
            fold["variants"][name] = {
                "all": evaluate(test, pt, "all"),
                "quiet": evaluate(test, pt, "quiet"),
                "selection_all": evaluate(cal, pc, "all"),
                "selection_quiet": evaluate(cal, pc, "quiet")}
            pred_frames.append(test.select("obj", "asof", "forecast_day", "y", "known", "quiet")
                               .with_columns(pl.Series("score", pt, dtype=pl.Float64),
                                             pl.lit(name).alias("model")))
            print(test_start, name, fold["variants"][name]["all"], flush=True)
        results["folds"].append(fold)
        (PATHS.tmp / "intrusion_six_hour_checkpoint.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    pl.concat(pred_frames).write_parquet(
        PATHS.features / f"intrusion_six_hour_{min_past_observed_hours}h_predictions.parquet")
    results["pooled"] = {}
    for name in MODEL_NAMES:
        results["pooled"][name] = {}
        for view in ("all", "quiet"):
            items = [f["variants"][name][view] for f in results["folds"]]
            vals = {key: sum(v[key] for v in items)
                    for key in ("candidates", "known_candidates", "positives", "alerts",
                                "hits", "unknown_alerts")}
            vals.update(precision_lower_bound=vals["hits"]/vals["alerts"] if vals["alerts"] else None,
                        recall_of_recorded_positives=vals["hits"]/vals["positives"] if vals["positives"] else None)
            results["pooled"][name][view] = vals
    (PATHS.reports / f"intrusion_six_hour_{min_past_observed_hours}h_coverage.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("POOLED", json.dumps(results["pooled"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
