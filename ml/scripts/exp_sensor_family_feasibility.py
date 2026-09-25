"""Count verifiable next-day sustained-failure examples by sensor family.

This is a feasibility audit, not a trained model or confirmed repair label.
"""
import json
import sys

from mkl import db, labels
from mkl.config import PATHS


def main() -> None:
    con = db.connect("4GB", 4)
    db.attach_parquet(con, "episodes")
    sensor = (PATHS.features / "sensor.parquet").as_posix()
    con.execute(f"""
      CREATE TEMP TABLE base AS
      SELECT ch, day, stype FROM read_parquet('{sensor}')
      WHERE day BETWEEN DATE '2019-01-01' AND DATE '2025-12-30'
    """)
    con.execute(f"""
      CREATE TEMP TABLE target AS
      SELECT DISTINCT ch, stype, CAST(t_start AS DATE) AS event_day,
             CAST(t_start AS DATE) - INTERVAL 1 DAY AS forecast_day
      FROM episodes
      WHERE {labels._VARIANT_FILTER['L6']}
        AND t_start >= TIMESTAMP '2019-01-01'
        AND t_start < TIMESTAMP '2026-01-01'
    """)
    rows = con.execute("""
      WITH base_stats AS (
        SELECT stype, count(*) AS candidate_days, count(DISTINCT ch) AS channels,
               count(*) FILTER (WHERE day BETWEEN DATE '2025-04-05'
                                     AND DATE '2025-12-30') AS test_days
        FROM base GROUP BY stype
      ), target_stats AS (
        SELECT t.stype,
               count(*) AS target_channel_days,
               count(*) FILTER (WHERE b.ch IS NOT NULL) AS forecastable_channel_days,
               count(*) FILTER (WHERE b.ch IS NOT NULL AND t.forecast_day
                 BETWEEN DATE '2025-04-05' AND DATE '2025-12-30') AS test_positives,
               count(*) FILTER (WHERE b.ch IS NOT NULL AND t.forecast_day
                 < DATE '2025-01-01') AS pre2025_positives
        FROM target t LEFT JOIN base b
          ON b.ch=t.ch AND b.day=t.forecast_day AND b.stype IS NOT DISTINCT FROM t.stype
        GROUP BY t.stype
      )
      SELECT coalesce(b.stype,t.stype) AS stype, b.candidate_days,
             b.channels, b.test_days, t.target_channel_days,
             t.forecastable_channel_days, t.pre2025_positives,
             t.test_positives
      FROM base_stats b FULL JOIN target_stats t
        ON b.stype IS NOT DISTINCT FROM t.stype
      ORDER BY forecastable_channel_days DESC NULLS LAST
    """).pl().to_dicts()
    by_year = con.execute("""
      SELECT b.stype, year(b.day) AS year,
             count(*) AS candidate_days,
             count(*) FILTER (WHERE t.ch IS NOT NULL) AS forecastable_positives
      FROM base b LEFT JOIN target t ON b.ch=t.ch AND b.day=t.forecast_day
        AND b.stype IS NOT DISTINCT FROM t.stype
      GROUP BY b.stype, year(b.day) ORDER BY b.stype, year
    """).pl().to_dicts()
    result = {"target": "L6: sustained, non-group bad-state episode on a recently live channel",
              "prediction": "end of day D for an episode starting on D+1",
              "note": "Counts of proxy outcomes with an actual D sensor record; no repair/false-alarm confirmation",
              "types": rows, "by_year": by_year}
    (PATHS.reports / "sensor_family_feasibility.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
