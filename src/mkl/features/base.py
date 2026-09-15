import duckdb

ROLLING_COLS = ("n_events", "n_alarms", "n_bad", "n_chatter_1min", "n_transitions")


def add_rolling_windows(con: duckdb.DuckDBPyConnection,
                        windows: tuple[int, ...] = (7, 30),
                        source: str = "daily_channel") -> None:
    """Оконные агрегаты ISA-18.2 и собственный ритм канала.

    Окна строго прошлые и включают текущие сутки целиком: фичи считаются на
    конец суток D, а метка смотрит в D+1…D+H, поэтому пересечения нет.
    """
    parts: list[str] = []
    for w in windows:
        for c in ROLLING_COLS:
            parts.append(
                f"sum({c}) OVER (PARTITION BY ch ORDER BY day "
                f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) AS {c}_w{w}"
            )
        parts.append(
            f"avg(CAST(n_events AS DOUBLE)) OVER (PARTITION BY ch ORDER BY day "
            f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) "
            f"AS n_events_mean_w{w}"
        )
        parts.append(
            f"stddev_pop(CAST(n_events AS DOUBLE)) OVER (PARTITION BY ch ORDER BY day "
            f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) "
            f"AS n_events_std_w{w}"
        )
        parts.append(
            f"count(*) OVER (PARTITION BY ch ORDER BY day "
            f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) AS n_active_days_w{w}"
        )
    rolling = ",\n           ".join(parts)

    con.execute(f"""
    CREATE OR REPLACE TABLE feat_base AS
    WITH r AS (
      SELECT ch, day, obj, stype, sys, picket,
             n_events, n_alarms, n_bad, n_ok, n_fire, n_intrusion,
             n_chatter_1min, n_transitions,
             max_gap_s, med_gap_s, val_mean, val_std, val_min, val_max,
             {rolling},
             max(CASE WHEN n_alarms > 0 THEN day END) OVER (
               PARTITION BY ch ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS last_alarm_day,
             max(CASE WHEN n_bad > 0 THEN day END) OVER (
               PARTITION BY ch ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS last_bad_day,
             max(max_gap_s) OVER (
               PARTITION BY ch ORDER BY day
               RANGE BETWEEN INTERVAL 89 DAY PRECEDING AND CURRENT ROW
             ) AS max_gap_q_s,
             avg(CAST(max_gap_s AS DOUBLE)) OVER (
               PARTITION BY ch ORDER BY day
               RANGE BETWEEN INTERVAL 89 DAY PRECEDING AND CURRENT ROW
             ) AS mean_gap_q_s,
             stddev_pop(CAST(max_gap_s AS DOUBLE)) OVER (
               PARTITION BY ch ORDER BY day
               RANGE BETWEEN INTERVAL 89 DAY PRECEDING AND CURRENT ROW
             ) AS std_gap_q_s
      FROM {source}
    )
    SELECT *,
           date_diff('day', last_alarm_day, day) AS days_since_last_alarm,
           date_diff('day', last_bad_day,   day) AS days_since_last_bad,
           CASE WHEN std_gap_q_s > 0
                THEN (max_gap_s - mean_gap_q_s) / std_gap_q_s END AS silence_z,
           CASE WHEN max_gap_q_s > 0
                THEN CAST(max_gap_s AS DOUBLE) / max_gap_q_s END AS silence_ratio,
           CASE WHEN n_events > 0
                THEN CAST(n_chatter_1min AS DOUBLE) / n_events END AS chatter_rate,
           CASE WHEN n_events > 0
                THEN CAST(n_alarms AS DOUBLE) / n_events END AS alarm_rate,
           row_number() OVER (PARTITION BY obj, day ORDER BY n_alarms DESC, ch)
             AS pareto_rank_obj
    FROM r
    """)
