import duckdb

from . import base, decay, external, lifecycle, relative, telemetry


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
    decay.add_decayed_intensity(con, source=source)
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

    # Затухающая интенсивность приклеивается последней: она посчитана отдельным
    # проходом по исходной панели и от остальных шагов не зависит.
    con.execute("""
    CREATE OR REPLACE TABLE feat_ext AS
    SELECT e.*, d.* EXCLUDE (ch, day)
    FROM feat_ext e LEFT JOIN feat_decay d ON d.ch = e.ch AND d.day = e.day
    """)


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
           -- Погодные колонки перечислены поимённо, и новые сюда не попадали
           -- сами: индекс увлажнения, сделанный ради головы подтопления, до
           -- неё не доезжал, потому что она живёт на объектном уровне.
           any_value(api_85) AS api_85, any_value(api_90) AS api_90,
           any_value(api_95) AS api_95,
           any_value(precip_7d) AS precip_7d, any_value(precip_14d) AS precip_14d,
           any_value(precip_30d) AS precip_30d,
           any_value(snowmelt_7d) AS snowmelt_7d,
           any_value(snowmelt_30d) AS snowmelt_30d,
           any_value(frost_intensity) AS frost_intensity,
           sum(n_flood_bins) AS n_flood_bins,
           sum(time_in_alarm_s) AS time_in_alarm_s,
           sum(time_in_bad_s)   AS time_in_bad_s,
           max(max_hold_alarm_s) AS max_hold_alarm_s,
           sum(n_standing_4h)   AS n_standing_4h,
           sum(n_stale_24h)     AS n_stale_24h,
           sum(n_many_bad)      AS n_many_bad,
           sum(n_battery_power) AS n_battery_power,
           sum(n_talk)          AS n_talk,
           sum(n_call)          AS n_call,
           sum(n_arm)           AS n_arm,
           sum(n_disarm)        AS n_disarm,
           max(chatter_psi_alarm) AS chatter_psi_alarm_max,
           avg(chatter_psi_alarm) AS chatter_psi_alarm_mean,
           -- Состояние охраны объекта на конец суток: берётся последнее по
           -- времени событие среди всех каналов объекта.
           arg_max(armed_eod, last_arm_ts) FILTER (WHERE armed_eod IS NOT NULL)
             AS armed_eod,
           max(max_alarm_10min) AS max_alarm_10min,
           avg(chatter_psi) AS chatter_psi_mean,
           max(chatter_psi) AS chatter_psi_max,
           avg(night_frac) AS night_frac_mean,
           avg(workhours_frac) AS workhours_frac_mean,
           any_value(obj_parent) AS obj_parent,
           CAST(any_value(obj_kind) = 'guardObject' AS INTEGER) AS is_guard_object
    FROM {source} WHERE obj IS NOT NULL
    GROUP BY obj, day
    """)
    add_load_concentration(con)
    add_arming_context(con)
    add_complex_context(con)



def add_load_concentration(con: duckdb.DuckDBPyConnection,
                           source: str = "feat_ext") -> None:
    """Концентрация нагрузки по каналам объекта — KPI EEMUA наоборот.

    То, что у оператора служит оценкой качества системы тревог, у нас
    становится признаком состояния объекта. frac_channels_bad отвечает лишь на
    вопрос «сколько каналов болеет», но не отличает «болеет один и сильно» от
    «болеет объект целиком»: при десяти каналах и десяти тревогах доля одна и
    та же, лежат ли все десять на одном канале или по одной на каждом.

    top1_share — доля тревог худшего канала, hhi — индекс Херфиндаля, то есть
    сумма квадратов долей: единица при полной концентрации, 1/n при равномерном
    распределении. n_channels_80pct — сколько каналов дают четыре пятых нагрузки.
    """
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE _conc AS
    WITH sh AS (
      SELECT obj, day, ch,
             CAST(n_alarms AS DOUBLE) / nullif(sum(n_alarms) OVER (PARTITION BY obj, day), 0)
               AS p_alarm,
             CAST(n_bad AS DOUBLE) / nullif(sum(n_bad) OVER (PARTITION BY obj, day), 0)
               AS p_bad
      FROM {source} WHERE obj IS NOT NULL
    ), ranked AS (
      SELECT obj, day, p_alarm, p_bad,
             sum(p_alarm) OVER (PARTITION BY obj, day ORDER BY p_alarm DESC
                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_alarm,
             row_number() OVER (PARTITION BY obj, day ORDER BY p_alarm DESC) AS rk
      FROM sh
    )
    SELECT obj, day,
           max(p_alarm)            AS top1_share_alarm,
           max(p_bad)              AS top1_share_bad,
           sum(p_alarm * p_alarm)  AS hhi_alarm,
           sum(p_bad * p_bad)      AS hhi_bad,
           count(*) FILTER (WHERE cum_alarm <= 0.8) + 1 AS n_channels_80pct,
           sum(p_alarm) FILTER (WHERE rk <= 10)         AS top10_share_alarm
    FROM ranked GROUP BY obj, day
    """)
    con.execute("""
    CREATE OR REPLACE TABLE feat_object AS
    SELECT o.*, c.* EXCLUDE (obj, day)
    FROM feat_object o LEFT JOIN _conc c ON c.obj = o.obj AND c.day = o.day
    """)


def add_arming_context(con: duckdb.DuckDBPyConnection) -> None:
    """Состояние охраны переносится на сутки, когда событий охраны не было.

    Объект стоит на охране неделями, а событие постановки одно. Без переноса
    признак был бы известен в единичные сутки и бесполезен. Перенос строго
    назад — берётся последнее известное состояние на конец текущих суток.

    Охрана есть только на 47 объектах из 78, поэтому NULL здесь означает
    «неизвестно», а не «снято с охраны», и заполнять его нулём нельзя.
    """
    con.execute("""
    CREATE OR REPLACE TABLE feat_object AS
    SELECT * EXCLUDE (armed_eod),
           last_value(armed_eod IGNORE NULLS) OVER (
             PARTITION BY obj ORDER BY day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS obj_armed,
           date_diff('day', last_value(CASE WHEN armed_eod IS NOT NULL THEN day END
                                       IGNORE NULLS) OVER (
             PARTITION BY obj ORDER BY day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), day)
             AS days_since_arm_event
    FROM feat_object
    """)


def add_complex_context(con: duckdb.DuckDBPyConnection) -> None:
    """Контекст комплекса для объектных голов.

    78 объектов входят в 16 комплексов. Авария питания или обрыв магистрали
    проявляются на комплексе целиком, поэтому состояние соседних объектов —
    внешний предиктор, которого в объектном наборе не было: привязка по
    префиксу тега ключа к иерархии не давала.

    Суммы берутся с исключением самого объекта: иначе признак наполовину
    повторяет собственные колонки строки и сообщает не «что вокруг», а «что
    у меня».
    """
    con.execute("""
    CREATE OR REPLACE TEMP TABLE _par AS
    SELECT obj_parent, day,
           count(*)             AS n_objects,
           sum(n_channels)      AS n_channels,
           sum(n_events)        AS n_events,
           sum(n_alarms)        AS n_alarms,
           sum(n_bad)           AS n_bad,
           sum(n_bad_w7)        AS n_bad_w7,
           sum(n_intrusion)     AS n_intrusion,
           sum(n_flood)         AS n_flood,
           sum(n_fire)          AS n_fire,
           count(*) FILTER (WHERE n_bad > 0) AS n_objects_bad
    FROM feat_object WHERE obj_parent IS NOT NULL
    GROUP BY obj_parent, day
    """)
    con.execute("""
    CREATE OR REPLACE TABLE feat_object AS
    SELECT o.*,
           p.n_objects - 1                      AS par_n_siblings,
           p.n_channels - o.n_channels          AS par_n_channels,
           p.n_events   - o.n_events            AS par_n_events,
           p.n_alarms   - o.n_alarms            AS par_n_alarms,
           p.n_bad      - o.n_bad               AS par_n_bad,
           p.n_bad_w7   - o.n_bad_w7            AS par_n_bad_w7,
           p.n_intrusion - o.n_intrusion        AS par_n_intrusion,
           p.n_flood    - o.n_flood             AS par_n_flood,
           p.n_fire     - o.n_fire              AS par_n_fire,
           p.n_objects_bad - CAST(o.n_bad > 0 AS INTEGER) AS par_n_objects_bad,
           CAST(p.n_objects_bad - CAST(o.n_bad > 0 AS INTEGER) AS DOUBLE)
             / nullif(p.n_objects - 1, 0)       AS par_frac_objects_bad,
           CAST(o.n_bad AS DOUBLE) / nullif(p.n_bad, 0)       AS par_share_bad,
           CAST(o.n_alarms AS DOUBLE) / nullif(p.n_alarms, 0) AS par_share_alarms,
           CAST(p.n_bad - o.n_bad AS DOUBLE)
             / nullif(p.n_channels - o.n_channels, 0)         AS par_bad_per_channel
    FROM feat_object o
    LEFT JOIN _par p ON p.obj_parent = o.obj_parent AND p.day = o.day
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
