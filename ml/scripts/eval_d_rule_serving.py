"""Replay the deployed D rule with its actual Monday threshold refresh.

The independent auditor refreshes every seven days from the first test day.
That is not the calendar used by ``rule_head.artifact`` in C1. This script
uses the production artifact selector, ranking, threshold and issued cooldown;
outcomes are joined only after a recommendation has been selected.

Example (from ml/, with the customer data bundle mounted under data/):
  python scripts/eval_d_rule_serving.py --end 2025-12-31 --splits 4 --test-days 28 \
    --out reports/d_rule_monday_2025h2.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

import polars as pl

from mkl import db, rule_head, second_ml_audit, serve, store
from mkl.config import PATHS


def replay(features: pl.DataFrame, outcomes: pl.DataFrame, events: pl.DataFrame,
           cfg: dict, start: dt.date, end: dt.date,
           source_last: dt.date, fold_days: int | None = None) -> dict:
    """Run the same head-local dispatch policy as C1, then attach final truth."""
    candidates = second_ml_audit.candidates(features, outcomes, "D", source_last)
    issued: list[tuple[int, dt.date]] = []
    selected: list[pl.DataFrame] = []
    refreshes: list[dict] = []
    previous_refresh = None
    for offset in range((end - start).days + 1):
        day = start + dt.timedelta(days=offset)
        refresh = rule_head.refresh_day(day)
        art = rule_head.artifact("D", cfg, day)
        if refresh != previous_refresh:
            refreshes.append({
                "refresh_day": str(refresh),
                "threshold_end": art["metadata"]["threshold_end"],
                "threshold": art["threshold"] if math.isfinite(art["threshold"]) else None,
                "feasible": rule_head.feasible(art),
                "selection": art["selection"],
            })
            previous_refresh = refresh
        frame = candidates.filter(pl.col("day") == day)
        if frame.is_empty():
            continue
        risk = frame[cfg["serving_rule"]].cast(pl.Float64).fill_nan(0.0).fill_null(0.0)
        ranked = serve._apply_budget(
            frame.select("ch", "obj", "day", "y").with_columns(risk.alias("risk")),
            int(cfg["budget_per_day"]), per_object=bool(cfg.get("budget_per_object")))
        ranked = ranked.with_columns(
            (pl.col("alert") & (pl.col("risk") >= art["threshold"])).alias("alert"))
        ranked = serve.apply_issued_cooldown(
            ranked, "ch", day, issued, cooldown_days=int(cfg["cooldown_days"]))
        issued.extend(ranked.filter(pl.col("alert")).select("ch", "day").iter_rows())
        selected.append(ranked)
    if not selected:
        raise ValueError("no candidate days in the requested period")
    picked = pl.concat(selected)
    observed_events = events.filter(pl.col("available_on") <= source_last)
    summary = second_ml_audit.summarize(candidates, picked, observed_events,
                                        start, end, int(cfg["horizon_days"]))
    folds = []
    if fold_days:
        first = start
        while first <= end:
            last = min(end, first + dt.timedelta(days=fold_days - 1))
            folds.append({"start": str(first), "end": str(last),
                          "summary": second_ml_audit.summarize(
                              candidates, picked, observed_events, first, last,
                              int(cfg["horizon_days"]))})
            first = last + dt.timedelta(days=1)
    return {"start": str(start), "end": str(end), "summary": summary,
            "refreshes": refreshes, "folds": folds}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--splits", type=int, required=True)
    parser.add_argument("--test-days", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.splits < 1 or args.test_days < 1:
        parser.error("splits and test-days must be positive")
    cfg = serve.load_heads()["D"]
    if cfg.get("serving_rule") != "n_bad_w7":
        raise ValueError("D no longer serves n_bad_w7; review this replay")
    start = args.end - dt.timedelta(days=args.splits * args.test_days - 1)
    con = db.connect()
    try:
        db.attach_parquet(con, "daily_channel", "episodes")
        source_last = con.execute("SELECT max(day) FROM daily_channel").fetchone()[0]
        if args.end + dt.timedelta(days=cfg["horizon_days"]) > source_last:
            raise ValueError("the requested outcomes have not matured")
        outcomes, events = second_ml_audit.build_outcomes(con, "D")
    finally:
        con.close()
    features = store.read_slice(cfg["feature_set"], start, args.end,
                                columns=["ch", "obj", "day", "stype", "n_bad_w7"])
    period = replay(features, outcomes, events, cfg, start, args.end,
                    source_last, fold_days=args.test_days)
    result = {
        "head": "D", "policy": "deployed_rule_n_bad_w7_monday_refresh",
        "target": "recorded_equipment_bad_state_or_alarm_in_next_7_days",
        "source_last_day": str(source_last),
        "data_sha256": {
            **{name: _sha256(PATHS.interim / f"{name}.parquet")
               for name in ("daily_channel", "episodes")},
            "sensor_features": _sha256(PATHS.features / f"{cfg['feature_set']}.parquet"),
        },
        "period": period,
        "caution": "Retrospective, previously inspected data. Prepared feature-store "
                   "as-of integrity and the D_observed_v1 observation contract "
                   "remain unverified. Compare with the old audit only on equal data hashes.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                   allow_nan=False), encoding="utf-8")
    s = period["summary"]
    print(f"D {start}..{args.end}: {s['hits']}/{s['alerts']} hits, "
          f"{s['unknown_alerts']} unknown, {s['episodes_caught']} episodes")
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
