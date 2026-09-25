"""Prototype a guard-only candidate refresh without rebuilding all features."""
from __future__ import annotations

import datetime as dt
import json
import time

import polars as pl

from mkl import db, guard_queue, store
from mkl.config import PATHS


def from_events(con, asof: dt.date) -> pl.DataFrame:
    """Object rows and last known guard state, using only events through asof."""
    return con.execute(f"""
      WITH today AS (
        SELECT DISTINCT obj FROM ev
        WHERE day = DATE '{asof}' AND obj IS NOT NULL
      ), guard_at_ts AS (
        SELECT obj, ts,
          CASE WHEN count(DISTINCT val_raw)=1
               THEN max(CASE WHEN val_raw='На охране' THEN 1 ELSE 0 END)
               ELSE NULL END AS armed
        FROM ev
        WHERE day <= DATE '{asof}' AND obj IS NOT NULL
          AND val_raw IN ('На охране', 'Снято с охраны')
        GROUP BY obj, ts
      ), last_guard AS (
        SELECT obj, ts, armed,
          row_number() OVER (PARTITION BY obj ORDER BY ts DESC) AS rn
        FROM guard_at_ts WHERE armed IS NOT NULL
      )
      SELECT t.obj, DATE '{asof}' AS day,
             g.armed AS obj_armed,
             date_diff('day', CAST(g.ts AS DATE), DATE '{asof}')
               AS days_since_arm_event
      FROM today t LEFT JOIN last_guard g ON g.obj=t.obj AND g.rn=1
      ORDER BY t.obj
    """).pl()


def main() -> None:
    dates = (dt.date(2024, 6, 17), dt.date(2025, 6, 16),
             dt.date(2026, 6, 29))
    con = db.connect("6GB", 4)
    db.attach_events(con)
    results = []
    for asof in dates:
        started = time.perf_counter()
        fast = from_events(con, asof)
        seconds = time.perf_counter() - started
        actual = store.read_slice("object", asof, asof, columns=[
            "obj", "day", "obj_armed", "days_since_arm_event"], f32=False)
        joined = actual.join(fast, on=["obj", "day"], how="full", suffix="_fast",
                             coalesce=True)
        state_mismatch = joined.filter(
            pl.col("obj_armed").fill_null(-1) !=
            pl.col("obj_armed_fast").fill_null(-1))
        age_mismatch = joined.filter(
            pl.col("days_since_arm_event").fill_null(-1) !=
            pl.col("days_since_arm_event_fast").fill_null(-1))
        old_c = guard_queue.current_candidates(actual)
        new_c = guard_queue.current_candidates(fast)
        results.append({"asof": str(asof), "seconds": round(seconds, 3),
                        "object_rows": actual.height, "fast_rows": fast.height,
                        "state_mismatch": state_mismatch.height,
                        "age_mismatch": age_mismatch.height,
                        "eligible_actual": old_c.height,
                        "eligible_fast": new_c.height,
                        "eligible_symmetric_difference": len(
                            set(old_c["obj"]) ^ set(new_c["obj"]))})
    con.close()
    result = {"source": "raw events through asof; prototype only",
              "results": results}
    (PATHS.reports / "guard_fast_candidates.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
