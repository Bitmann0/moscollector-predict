"""Audit C's day-level proxy against alarm flags and guard state at event time.

Candidate definitions are sensitivity analyses, not confirmed intrusion labels.
Same-second guard changes are ambiguous and never counted as known armed.
"""
import json
import sys

from mkl import db, labels
from mkl.config import ARM_STATES, INTRUSION_STATES, PATHS

def _in(values) -> str:
    return "(" + ", ".join("'" + v.replace("'", "''") + "'"
                            for v in sorted(values)) + ")"


def build_guard_timeline(con, source: str) -> None:
    """Source is a trusted SQL relation containing the full control history."""
    con.execute(f"""
      CREATE TEMP TABLE guard AS
      SELECT obj, ts AS guard_ts,
             CASE WHEN count(DISTINCT val_raw) = 1
                  THEN max(CASE WHEN val_raw = 'На охране' THEN 1 ELSE 0 END)
                  ELSE NULL END AS armed
      FROM {source}
      WHERE obj IS NOT NULL AND ts < TIMESTAMP '2026-01-01'
        AND val_raw IN {_in(ARM_STATES)} GROUP BY obj, ts
    """)


def attach_guard_state(con) -> None:
    """Join the last control to each intrusion; simultaneous events are unknown."""
    con.execute("""
      CREATE TEMP TABLE at_time AS
      SELECT i.*, g.guard_ts,
             CASE WHEN g.guard_ts = i.ts THEN NULL ELSE g.armed END AS armed_at_event
      FROM intrusion i ASOF LEFT JOIN guard g ON i.obj = g.obj AND i.ts >= g.guard_ts
    """)


def main() -> None:
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    src = (PATHS.interim / "events_year=*.parquet").as_posix()
    # Sparse control events retain the full history for initialization in 2023.
    build_guard_timeline(con, f"read_parquet('{src}')")
    print("guard timeline built", flush=True)
    con.execute(f"""
      CREATE TEMP TABLE intrusion AS
      SELECT obj, ch, ts, day, stype, val_raw, alarm
      FROM read_parquet('{src}')
      WHERE obj IS NOT NULL AND day BETWEEN DATE '2023-01-01' AND DATE '2025-12-31'
        AND val_raw IN {_in(INTRUSION_STATES)} AND alarm
    """)
    attach_guard_state(con)
    con.execute(f"CREATE TEMP TABLE armed_day AS {labels._ARMED_SQL}")
    con.execute("""
      CREATE TEMP TABLE exact_days AS
      SELECT i.obj, i.day,
             bool_or(a.armed = 1) AS alarm_flag_and_eod,
             bool_or(i.armed_at_event = 1) AS alarm_flag_and_asof,
             bool_or(i.armed_at_event IS NULL) AS unknown_guard_candidate,
             bool_or(i.armed_at_event = 1 AND i.stype IN
               ('Датчик движения', 'КД Дверь', 'КД Люк', '9-секционный люк',
                'Стекло', 'Состояние УИР-Р')) AS security_types_and_asof
      FROM at_time i LEFT JOIN armed_day a USING (obj, day) GROUP BY i.obj, i.day
    """)
    con.execute("""
      CREATE TEMP TABLE current_days AS
      SELECT DISTINCT d.obj, d.day FROM daily_channel d JOIN armed_day a USING (obj, day)
      WHERE d.day BETWEEN DATE '2023-01-01' AND DATE '2025-12-31'
        AND d.n_intrusion > 0 AND d.n_alarms > 0 AND a.armed = 1
    """)
    con.execute("""
      CREATE TEMP TABLE audit_days AS
      SELECT coalesce(c.obj,e.obj) AS obj, coalesce(c.day,e.day) AS day,
             c.obj IS NOT NULL AS current_proxy,
             coalesce(e.alarm_flag_and_eod, false) AS alarm_flag_and_eod,
             coalesce(e.alarm_flag_and_asof, false) AS alarm_flag_and_asof,
             coalesce(e.unknown_guard_candidate, false) AS unknown_guard_candidate,
             coalesce(e.security_types_and_asof, false) AS security_types_and_asof
      FROM current_days c FULL JOIN exact_days e USING (obj,day)
    """)
    names = ("current_proxy", "alarm_flag_and_eod", "alarm_flag_and_asof",
             "security_types_and_asof")
    counts = {}
    for name in names:
        vals = con.execute(f"""SELECT count(*) FILTER (WHERE {name}),
          count(*) FILTER (WHERE {name} AND current_proxy),
          count(*) FILTER (WHERE {name} AND NOT current_proxy),
          count(*) FILTER (WHERE NOT {name} AND current_proxy) FROM audit_days""").fetchone()
        counts[name] = dict(zip(("positive_object_days", "overlap_current", "added", "removed"), vals))
    by_type = con.execute("""
      SELECT stype, count(*) AS events, count(*) FILTER (WHERE armed_at_event=1) AS armed,
             count(*) FILTER (WHERE armed_at_event=0) AS disarmed,
             count(*) FILTER (WHERE armed_at_event IS NULL) AS unknown
      FROM at_time GROUP BY stype ORDER BY events DESC
    """).pl().to_dicts()
    result = {"note": "2023-2025 target sensitivity; no confirmed incident outcomes",
              "counts": counts, "alarm_events_by_type": by_type,
              "same_second_guard_alarm_events": con.execute(
                  "SELECT count(*) FROM at_time WHERE ts=guard_ts").fetchone()[0]}
    con.execute("""
      CREATE TEMP TABLE guard_check AS
      SELECT a.obj, a.day, a.armed AS panel_armed, g.armed AS raw_armed
      FROM armed_day a ASOF LEFT JOIN guard g
        ON a.obj=g.obj AND (CAST(a.day AS TIMESTAMP) + INTERVAL 1 DAY) > g.guard_ts
      WHERE a.day BETWEEN DATE '2023-01-01' AND DATE '2025-12-31'
    """)
    result["guard_eod_consistency"] = con.execute("""
      SELECT count(*) AS object_days,
             count(*) FILTER (WHERE panel_armed IS DISTINCT FROM raw_armed) AS mismatches
      FROM guard_check
    """).pl().to_dicts()[0]
    result["per_year"] = con.execute("""
      SELECT year(day) AS year, sum(current_proxy::INTEGER)::BIGINT AS current_proxy,
             sum(alarm_flag_and_asof::INTEGER)::BIGINT AS exact_event_time,
             sum(security_types_and_asof::INTEGER)::BIGINT AS security_event_time
      FROM audit_days GROUP BY year(day) ORDER BY year
    """).pl().to_dicts()
    con.execute(f"COPY audit_days TO '{(PATHS.features / 'intrusion_target_audit.parquet').as_posix()}' (FORMAT PARQUET)")
    (PATHS.reports / "intrusion_label_timing_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    con.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
