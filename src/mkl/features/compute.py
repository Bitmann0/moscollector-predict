import duckdb

from . import base, external, lifecycle, relative, telemetry


def build_all(con: duckdb.DuckDBPyConnection,
              windows: tuple[int, ...] = (7, 30),
              radius_seg: int = 1,
              with_weather: bool = True,
              with_episode_history: bool = True,
              source: str = "daily_channel") -> None:
    """Единственный путь получения фич — и на обучении, и на инференсе.

    Любой другой способ посчитать фичи создаёт train/serve skew, поэтому
    обучение и скоринг обязаны вызывать именно эту функцию.
    """
    base.add_rolling_windows(con, windows=windows, source=source)
    telemetry.add_value_features(con, source="feat_base")
    telemetry.add_value_drift(con, source="feat_value")
    con.execute("CREATE OR REPLACE TABLE feat_base AS SELECT * FROM feat_value")
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius_seg=radius_seg)
    relative.add_object_context(con)
    lifecycle.add_lifecycle_features(con, source="feat_objctx")
    if with_episode_history:
        lifecycle.add_episode_history(con, source="feat_full")
        lifecycle.add_weekday_profile(con, source="feat_ephist")
        external.add_calendar(con, source="feat_dow")
    else:
        lifecycle.add_weekday_profile(con, source="feat_full")
        external.add_calendar(con, source="feat_dow")
    # Вызывается всегда: без кэша погоды колонки создаются пустыми, чтобы схема
    # фичестора не зависела от доступности внешнего источника.
    if with_weather:
        external.add_weather(con)
    else:
        nulls = ", ".join(
            f"CAST(NULL AS DOUBLE) AS {c}" for c in external.WEATHER_COLUMNS
        )
        con.execute(f"CREATE OR REPLACE TABLE feat_ext AS SELECT *, {nulls} FROM feat_ext")


def build_object_level(con: duckdb.DuckDBPyConnection, source: str = "feat_ext") -> None:
    """Агрегат до объекта — сущность голов A′ (массовый отказ) и C (НСД)."""
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_object AS
    SELECT obj, day,
           count(*)            AS n_channels,
           sum(n_events)       AS n_events,
           sum(n_alarms)       AS n_alarms,
           sum(n_bad)          AS n_bad,
           sum(n_fire)         AS n_fire,
           sum(n_intrusion)    AS n_intrusion,
           sum(n_flood)        AS n_flood,
           sum(n_on)           AS n_pump_on,
           sum(n_all_pumps)    AS n_all_pumps,
           count(*) FILTER (WHERE stype = 'Состояние насоса')          AS n_pumps,
           sum(n_transitions) FILTER (WHERE stype = 'Состояние насоса') AS pump_switches,
           sum(n_bad) FILTER (WHERE stype = 'Состояние насоса')         AS pump_bad,
           sum(n_chatter_1min) AS n_chatter_1min,
           sum(n_transitions)  AS n_transitions,
           sum(n_alarms_w7)    AS n_alarms_w7,
           sum(n_alarms_w30)   AS n_alarms_w30,
           sum(n_bad_w7)       AS n_bad_w7,
           sum(n_bad_w30)      AS n_bad_w30,
           sum(n_events_w7)    AS n_events_w7,
           sum(n_events_w30)   AS n_events_w30,
           sum(n_chatter_1min_w7) AS n_chatter_1min_w7,
           avg(silence_ratio)  AS silence_ratio_mean,
           max(silence_ratio)  AS silence_ratio_max,
           avg(silence_z)      AS silence_z_mean,
           max(silence_z)      AS silence_z_max,
           avg(age_days)       AS age_days_mean,
           min(age_days)       AS age_days_min,
           avg(days_since_last_bad) AS days_since_last_bad_mean,
           min(days_since_last_bad) AS days_since_last_bad_min,
           sum(n_prior_failures)    AS n_prior_failures,
           count(*) FILTER (WHERE n_bad > 0) AS n_channels_bad,
           CAST(count(*) FILTER (WHERE n_bad > 0) AS DOUBLE) / count(*) AS frac_channels_bad,
           any_value(dow) AS dow, any_value(month) AS month,
           any_value(is_weekend) AS is_weekend, any_value(is_holiday) AS is_holiday,
           any_value(doy_sin) AS doy_sin, any_value(doy_cos) AS doy_cos,
           any_value(t_mean) AS t_mean, any_value(t_min) AS t_min,
           any_value(t_max) AS t_max, any_value(precip_mm) AS precip_mm,
           any_value(snow_depth_cm) AS snow_depth_cm, any_value(t_range) AS t_range,
           any_value(precip_24h) AS precip_24h, any_value(precip_48h) AS precip_48h,
           any_value(precip_72h) AS precip_72h, any_value(snowmelt) AS snowmelt,
           any_value(snow_delta) AS snow_delta,
           sum(n_flood_bins) AS n_flood_bins,
           max(max_alarm_10min) AS max_alarm_10min,
           avg(chatter_psi) AS chatter_psi_mean,
           max(chatter_psi) AS chatter_psi_max,
           avg(night_frac) AS night_frac_mean,
           avg(workhours_frac) AS workhours_frac_mean
    FROM {source} WHERE obj IS NOT NULL
    GROUP BY obj, day
    """)


def build_segment_level(con: duckdb.DuckDBPyConnection, source: str = "feat_ext") -> None:
    """Агрегат до участка (объект x километровый сегмент) — сущность головы B."""
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_segment AS
    SELECT obj, seg, day,
           count(*)      AS n_channels,
           sum(n_events) AS n_events,
           sum(n_alarms) AS n_alarms,
           sum(n_fire)   AS n_fire,
           sum(n_bad)    AS n_bad,
           sum(n_alarms_w7)  AS n_alarms_w7,
           sum(n_alarms_w30) AS n_alarms_w30,
           sum(n_events_w7)  AS n_events_w7,
           max(val_max)  AS temp_max,
           avg(val_mean) AS temp_mean,
           max(val_max) - min(val_min) AS temp_range,
           max(nbr_val_max) AS nbr_temp_max,
           avg(val_minus_seg_mean) AS temp_dev_mean,
           max(abs(val_minus_seg_mean)) AS temp_dev_max,
           count(*) FILTER (WHERE stype = 'Тепловой датчик')    AS n_heat_sensors,
           count(*) FILTER (WHERE stype = 'Датчик дыма')        AS n_smoke_sensors,
           count(*) FILTER (WHERE stype = 'Газовый датчик')     AS n_gas_sensors,
           count(*) FILTER (WHERE stype = 'Датчик температуры') AS n_temp_sensors,
           any_value(dow) AS dow, any_value(month) AS month,
           any_value(is_weekend) AS is_weekend, any_value(is_holiday) AS is_holiday,
           any_value(doy_sin) AS doy_sin, any_value(doy_cos) AS doy_cos,
           any_value(t_mean) AS t_mean, any_value(t_max) AS t_max,
           any_value(precip_mm) AS precip_mm, any_value(t_range) AS t_range
    FROM {source} WHERE obj IS NOT NULL
    GROUP BY obj, seg, day
    """)
