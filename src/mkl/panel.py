import duckdb

from .config import BAD_STATES, FIRE_STATES, INTRUSION_STATES, OK_STATES


def _in(states: frozenset[str]) -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(states)) + ")"


def build_daily_channel(con: duckdb.DuckDBPyConnection, source: str = "ev") -> None:
    """Суточная панель канал x сутки — основа всех дальнейших расчётов.

    Сводит 313 млн событий к ~15 млн строк, после чего оконные фичи считаются
    по календарю суток, а не по событиям.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE daily_channel AS
    WITH e AS (
      SELECT ch, day, ts, alarm, val_raw, val_num, obj, stype, sys, picket,
             epoch(ts) - lag(epoch(ts))    OVER (PARTITION BY ch, day ORDER BY ts) AS gap_s,
             epoch(ts) - lag(epoch(ts), 2) OVER (PARTITION BY ch, day ORDER BY ts) AS gap3_s,
             CASE WHEN val_raw IS DISTINCT FROM
                  lag(val_raw) OVER (PARTITION BY ch, day ORDER BY ts)
                  THEN 1 ELSE 0 END AS is_transition
      FROM {source}
    )
    SELECT ch, day,
           any_value(obj) AS obj, any_value(stype) AS stype,
           any_value(sys) AS sys, any_value(picket) AS picket,
           count(*)                                                    AS n_events,
           count(*) FILTER (WHERE alarm)                               AS n_alarms,
           count(*) FILTER (WHERE val_raw IN {_in(BAD_STATES)})        AS n_bad,
           count(*) FILTER (WHERE val_raw IN {_in(OK_STATES)})         AS n_ok,
           count(*) FILTER (WHERE val_raw IN {_in(FIRE_STATES)})       AS n_fire,
           count(*) FILTER (WHERE val_raw IN {_in(INTRUSION_STATES)})  AS n_intrusion,
           count(*) FILTER (WHERE val_raw = 'Затоплен')                AS n_flood,
           count(*) FILTER (WHERE val_raw = 'Включен')                 AS n_on,
           count(*) FILTER (WHERE val_raw = 'Работают все насосы в АНС') AS n_all_pumps,
           CAST(sum(is_transition) AS BIGINT)                          AS n_transitions,
           count(*) FILTER (WHERE gap3_s IS NOT NULL AND gap3_s <= 60) AS n_chatter_1min,
           CAST(coalesce(max(gap_s), 0) AS BIGINT)                     AS max_gap_s,
           median(gap_s)                                               AS med_gap_s,
           min(val_num) AS val_min, max(val_num) AS val_max,
           avg(val_num) AS val_mean, stddev_pop(val_num) AS val_std
    FROM e
    GROUP BY ch, day
    """)
