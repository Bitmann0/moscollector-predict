"""Raw event timing + daily telemetry for next-day guarded intrusion alarms.

This tests the multimodal/sequence idea, not the published APT architecture.
Every raw-event feature uses only its own day; the label is the next day.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from exp_intrusion_sequence import with_history
from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import ARM_STATES, INTRUSION_STATES, PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)


def armed_intrusion_days(con) -> set[tuple[str, dt.date]]:
    """Actual alarm days, including days without an eligible preceding label row."""
    rows = con.execute(f"""
        SELECT DISTINCT d.obj, d.day
        FROM daily_channel d JOIN ({labels._ARMED_SQL}) a
          ON a.obj = d.obj AND a.day = d.day
        WHERE d.n_intrusion > 0 AND d.n_alarms > 0 AND a.armed = 1
    """).fetchall()
    return set(rows)


def _in(values: frozenset[str]) -> str:
    return "(" + ", ".join("'" + v.replace("'", "''") + "'"
                            for v in sorted(values)) + ")"


def event_day(year: int) -> pl.DataFrame:
    source = PATHS.interim / f"events_year={year}.parquet"
    cache = PATHS.features / f"intrusion_events_v1_{year}.parquet"
    if not cache.exists() or cache.stat().st_mtime < source.stat().st_mtime:
        intrusion = _in(INTRUSION_STATES)
        arm = _in(ARM_STATES)
        con = db.connect()
        con.execute(f"""
        COPY (
          WITH relevant AS (
            SELECT obj, day, ch, ts, event_id, val_raw, alarm,
                   CASE WHEN val_raw IN {intrusion} AND alarm THEN 3
                        WHEN val_raw IN {intrusion} THEN 2
                        WHEN val_raw IN {arm} THEN 1
                        ELSE 4 END AS token
            FROM read_parquet('{source.as_posix()}')
            WHERE obj IS NOT NULL
              AND (val_raw IN {intrusion} OR val_raw IN {arm} OR alarm)
          ), ranked AS (
            SELECT *, row_number() OVER (
              PARTITION BY obj, day ORDER BY ts DESC, event_id DESC,
                                           val_raw DESC, ch DESC) AS rn
            FROM relevant
          )
          SELECT obj, day,
                 count(*) AS evt_relevant,
                 count(*) FILTER (WHERE token = 2) AS evt_intrusion_unalarmed,
                 count(*) FILTER (WHERE token = 3) AS evt_intrusion_alarm,
                 count(DISTINCT ch) FILTER (WHERE token IN (2, 3))
                   AS evt_intrusion_channels,
                 count(DISTINCT hour(ts)) FILTER (WHERE token IN (2, 3))
                   AS evt_intrusion_hours,
                 count(*) FILTER (WHERE token IN (2, 3) AND hour(ts) < 6)
                   AS evt_intrusion_00_06,
                 count(*) FILTER (WHERE token IN (2, 3) AND hour(ts) BETWEEN 6 AND 11)
                   AS evt_intrusion_06_12,
                 count(*) FILTER (WHERE token IN (2, 3) AND hour(ts) BETWEEN 12 AND 17)
                   AS evt_intrusion_12_18,
                 count(*) FILTER (WHERE token IN (2, 3) AND hour(ts) >= 18)
                   AS evt_intrusion_18_24,
                 count(*) FILTER (WHERE token = 1) AS evt_guard_states,
                 count(*) FILTER (WHERE token = 4) AS evt_other_alarm,
                 max(CASE WHEN rn = 1 THEN token END) AS evt_last_token_1,
                 max(CASE WHEN rn = 2 THEN token END) AS evt_last_token_2,
                 max(CASE WHEN rn = 3 THEN token END) AS evt_last_token_3,
                 max(CASE WHEN rn = 1 THEN hour(ts) END) AS evt_last_hour,
                 max(CASE WHEN rn = 1 THEN minute(ts) END) AS evt_last_minute,
                 date_diff('second',
                   max(CASE WHEN rn = 2 THEN ts END),
                   max(CASE WHEN rn = 1 THEN ts END)) AS evt_last_gap_s,
                 date_diff('second',
                   min(ts) FILTER (WHERE token IN (2, 3)),
                   max(ts) FILTER (WHERE token IN (2, 3)))
                   AS evt_intrusion_span_s
          FROM ranked GROUP BY obj, day
        ) TO '{cache.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        con.close()
        print(f"built {cache.name}", flush=True)
    return pl.read_parquet(cache)


def score(fit: dict, data: pl.DataFrame, split, budget: int,
          event_days: set[tuple[str, dt.date]]) -> dict:
    block = data.filter((pl.col("day") >= split.test_start) &
                        (pl.col("day") <= split.test_end))
    p = fit["model"].predict_proba(
        train._matrix(block, fit["feature_names"]))[:, 1]
    y, days = block["y"].to_numpy(), block["day"].to_numpy()
    daily = metrics.daily_budget_summary(y, p, days, budget)
    result = {"n": len(y), "positives": int(y.sum()),
            "pr_auc": metrics.pr_auc(y, p),
            "daily_precision": daily["daily_precision_at_k"],
            "daily_recall": daily["daily_recall_at_k"]}
    quiet = np.asarray([
        not any((obj, day - dt.timedelta(days=offset)) in event_days
                for offset in range(7))
        for obj, day in block.select("obj", "day").iter_rows()], dtype=bool)
    onset = metrics.daily_budget_summary(y[quiet], p[quiet], days[quiet], budget)
    result["quiet_onset"] = {
        "n": int(quiet.sum()), "positives": int(y[quiet].sum()),
        "pr_auc": metrics.pr_auc(y[quiet], p[quiet]),
        "daily_precision": onset["daily_precision_at_k"],
        "daily_recall": onset["daily_recall_at_k"]}
    return result


def main() -> None:
    cfg = serve.load_heads()["C"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    event_days = armed_intrusion_days(con)
    con.close()
    base = store.read_slice("object", START, END)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    event = pl.concat([event_day(y) for y in (2023, 2024, 2025)])
    fused = base.join(event, on=["obj", "day"], how="left")
    count_cols = [c for c in event.columns if c.startswith("evt_") and
                  not c.startswith("evt_last_") and c != "evt_intrusion_span_s"]
    fused = fused.with_columns([pl.col(c).fill_null(0) for c in count_cols])
    fused_history, _ = with_history(fused)
    variants = {"baseline": base, "history": with_history(base)[0],
                "event_fusion": fused, "event_fusion_history": fused_history}
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"note": "2025 folds already used for earlier C model selection",
               "event_features": [c for c in event.columns if c.startswith("evt_")],
               "folds": []}
    for split in splits:
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, frame in variants.items():
            fit = train.run("C", frame, lab, [split],
                            params=train.params_for(cfg, "lgbm"),
                            budget_per_day=cfg["budget_per_day"], backend="lgbm")
            if fit["model"] is None:
                raise ValueError(f"{name}: empty fit")
            data = frame.join(lab, on=["obj", "day"]).sort(["day", "obj"])
            row[name] = score(fit, data, split, cfg["budget_per_day"],
                              event_days)
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    results["mean"] = {
        name: {metric: float(np.mean([row[name][metric] for row in results["folds"]]))
               for metric in ("pr_auc", "daily_precision", "daily_recall")}
        for name in variants}
    results["mean_quiet_onset"] = {
        name: {metric: float(np.mean([
            row[name]["quiet_onset"][metric] for row in results["folds"]]))
               for metric in ("pr_auc", "daily_precision", "daily_recall")}
        for name in variants}
    (PATHS.reports / "intrusion_event_fusion_experiment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
