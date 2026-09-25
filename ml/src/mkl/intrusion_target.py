"""Versioned event-time proxy for a guarded intrusion alarm.

This labels recorded SMVU alarms, not confirmed unauthorised access. Missing
future observation or unresolved guard state is unknown, not a negative.
The function accepts trusted DuckDB relation names created by the caller.
"""
from __future__ import annotations

import datetime as dt
import re

import duckdb

from .config import ARM_STATES, INTRUSION_STATES


def _quoted(values: frozenset[str]) -> str:
    return "(" + ", ".join("'" + x.replace("'", "''") + "'"
                             for x in sorted(values)) + ")"


def build(
    con: duckdb.DuckDBPyConnection,
    start: dt.date,
    end: dt.date,
    *,
    events: str = "ev",
    object_days: str = "object_days",
    target_table: str = "label_intrusion_eventtime",
    event_days_table: str = "intrusion_eventtime_days",
) -> None:
    """Build next-day object labels and actual alarm days from timestamped events.

    `events` must have obj, ts, val_raw, alarm; `object_days` must have obj,
    day. Object-day presence is a *weak* observation proxy and does not prove
    that every intrusion sensor reported normally. The final observed day is
    censored because its next day has no data.
    """
    if start > end:
        raise ValueError("start must not exceed end")
    for name in (events, object_days, target_table, event_days_table):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid SQL relation name: {name!r}")
    stop = end + dt.timedelta(days=1)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE _eventtime_guard AS
      SELECT obj, ts AS guard_ts,
             CASE WHEN count(DISTINCT val_raw)=1
                  THEN max(CASE WHEN val_raw='На охране' THEN 1 ELSE 0 END)
                  ELSE NULL END AS armed
      FROM {events}
      WHERE obj IS NOT NULL AND ts < DATE '{stop + dt.timedelta(days=1)}'
        AND val_raw IN {_quoted(ARM_STATES)}
      GROUP BY obj, ts
    """)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE _eventtime_alarm AS
      SELECT i.obj, i.ts,
             CASE WHEN g.guard_ts=i.ts THEN NULL ELSE g.armed END AS armed_at_event
      FROM (SELECT obj, ts FROM {events}
            WHERE obj IS NOT NULL
              AND ts < DATE '{stop + dt.timedelta(days=1)}'
              AND alarm AND val_raw IN {_quoted(INTRUSION_STATES)}) i
      ASOF LEFT JOIN _eventtime_guard g
        ON i.obj=g.obj AND i.ts>=g.guard_ts
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE {event_days_table} AS
      SELECT obj, CAST(ts AS DATE) AS day,
             bool_or(armed_at_event=1) AS positive,
             bool_or(armed_at_event IS NULL) AS unresolved,
             count(*) AS n_alarm_events
      FROM _eventtime_alarm GROUP BY obj, CAST(ts AS DATE)
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE {target_table} AS
      WITH days AS (SELECT DISTINCT obj, day FROM {object_days}
                    WHERE obj IS NOT NULL),
      candidates AS (SELECT obj, day FROM days
                    WHERE day BETWEEN DATE '{start}' AND DATE '{end}')
      SELECT c.obj, c.day,
             coalesce(e.positive, false)::INTEGER AS y,
             (coalesce(e.positive, false) OR
               (o.obj IS NOT NULL AND NOT coalesce(e.unresolved, false))) AS known,
             o.obj IS NOT NULL AS next_day_object_observed,
             coalesce(e.unresolved, false) AS unresolved_alarm_tomorrow
      FROM candidates c
      LEFT JOIN days o ON o.obj=c.obj AND o.day=c.day + INTERVAL 1 DAY
      LEFT JOIN {event_days_table} e
        ON e.obj=c.obj AND e.day=c.day + INTERVAL 1 DAY
    """)
