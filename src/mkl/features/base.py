import duckdb

# Время в тревоге и в плохом состоянии катится окнами наравне со счётчиками:
# деградация видна не в числе срабатываний, а в том, что канал всё дольше
# остаётся в ненормальном состоянии.
ROLLING_COLS = ("n_events", "n_alarms", "n_bad", "n_chatter_1min", "n_transitions",
                "time_in_alarm_s", "time_in_bad_s", "n_standing_4h")


CUSUM_COLS = ("n_events", "n_alarms", "n_bad", "time_in_alarm_s")


def dev90_parts(cols=CUSUM_COLS) -> list[str]:
    """Отклонение от собственного 90-суточного уровня. Первый шаг CUSUM."""
    return [
        f"CAST({c} AS DOUBLE) - avg(CAST({c} AS DOUBLE)) OVER "
        f"(PARTITION BY ch ORDER BY day "
        f"RANGE BETWEEN INTERVAL 89 DAY PRECEDING AND CURRENT ROW) AS dev90_{c}"
        for c in cols
    ]


def cusum_parts(cols=CUSUM_COLS) -> list[str]:
    """CUSUM без рекурсии: накопленное отклонение от собственного уровня.

    Ловит малый, но устойчивый сдвиг среднего — тот, которого z-оценка в
    скользящем окне не видит, потому что окно уезжает вместе со сдвигом.
    Вычитание 90-суточного уровня обязательно: без него сумма растёт у любого
    активного канала и меряет возраст, а не дрейф.

    Окна строго по прошлому, включая текущие сутки: признак готов к концу суток
    t, метка живёт в t+1.
    """
    return [
        f"sum(dev90_{c}) OVER (PARTITION BY ch ORDER BY day "
        f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cusum_{c}"
        for c in cols
    ]


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
        for c, agg in (("chatter_psi", "avg"), ("chatter_psi", "max"),
                       ("night_frac", "avg"), ("max_10min", "max"),
                       ("n_flood_bins", "sum")):
            parts.append(
                f"{agg}(CAST({c} AS DOUBLE)) OVER (PARTITION BY ch ORDER BY day "
                f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) "
                f"AS {c}_{agg}_w{w}"
            )
    parts += cusum_parts()
    dev90 = ",\n             ".join(dev90_parts())
    rolling = ",\n           ".join(parts)

    # Признаки ускорения осмысленны только при наличии обоих окон.
    accel = ""
    if 7 in windows and 30 in windows:
        accel = """,
           -- Отношение сегодняшней активности к собственной базовой линии.
           -- Аудит 250 отказов показал рост активности перед отказом
           -- в 54% случаев, поэтому признак задан явно, а не сырыми счётчиками.
           CASE WHEN n_events_mean_w30 > 0
                THEN n_events / n_events_mean_w30 END AS events_vs_own_w30,
           CASE WHEN n_events_std_w30 > 0
                THEN (n_events - n_events_mean_w30) / n_events_std_w30
                END AS events_z_own_w30,
           CASE WHEN n_events_w30 > 0
                THEN (n_events_w7 / 7.0) / (n_events_w30 / 30.0) END AS events_accel,
           CASE WHEN n_alarms_w30 > 0
                THEN (n_alarms_w7 / 7.0) / (n_alarms_w30 / 30.0) END AS alarms_accel,
           CASE WHEN n_bad_w30 > 0
                THEN (n_bad_w7 / 7.0) / (n_bad_w30 / 30.0) END AS bad_accel,
           CASE WHEN n_active_days_w30 > 0
                THEN CAST(n_active_days_w7 AS DOUBLE) / n_active_days_w30
                END AS activity_days_ratio,
           CASE WHEN prev_gap_days_mean_w30 > 0
                THEN prev_gap_days / prev_gap_days_mean_w30
                END AS gap_vs_own_rhythm"""

    con.execute(f"""
    CREATE OR REPLACE TABLE feat_base AS
    WITH g AS (
      -- Ритм отчётности канала в сутках. Строго прошлое: это разрыв, который
      -- уже закончился сегодня, тогда как метка молчания спрашивает про
      -- разрыв, начинающийся завтра. Вынесено в отдельный шаг: DuckDB
      -- не допускает вложенных оконных функций.
      SELECT *, date_diff('day', lag(day) OVER (PARTITION BY ch ORDER BY day), day)
               AS prev_gap_days,
             {dev90}
      FROM {source}
    ), r AS (
      SELECT ch, day, obj, obj_parent, obj_kind, stype, sys, picket,
             n_events, n_alarms, n_bad, n_ok, n_fire, n_intrusion,
             n_flood, n_on, n_all_pumps,
             val_ok_min, val_ok_max, val_ok_mean, val_ok_med, n_val_ok, n_val_bad,
             n_saturated, n_distinct_vals, n_val_nonzero, n_val_gt005,
             n_val_gt02, max_flat_run, max_10min, max_alarm_10min,
             n_flood_bins, chatter_psi, chatter_runs, last_alarm_hour,
             night_frac, workhours_frac, night_alarm_frac, n_active_hours,
             n_chatter_1min, n_transitions,
             chatter_psi_alarm, time_in_alarm_s, time_in_bad_s,
             max_hold_alarm_s, med_hold_alarm_s, n_standing_4h, n_stale_24h,
             n_many_bad, n_devices_ok, n_battery_power, n_talk, n_call,
             n_arm, n_disarm, armed_eod, last_arm_ts,
             max_gap_s, med_gap_s, val_mean, val_std, val_min, val_max,
             prev_gap_days,
             avg(CAST(prev_gap_days AS DOUBLE))
               OVER (PARTITION BY ch ORDER BY day
                     RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW)
               AS prev_gap_days_mean_w30,
             max(prev_gap_days)
               OVER (PARTITION BY ch ORDER BY day
                     RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW)
               AS prev_gap_days_max_w30,
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
      FROM g
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
             AS pareto_rank_obj{accel}
    FROM r
    """)
