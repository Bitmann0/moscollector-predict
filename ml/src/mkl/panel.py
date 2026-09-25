import duckdb

from .config import (BAD_STATES, FIRE_STATES, GAS_SATURATION,
                     INTRUSION_STATES, OK_STATES, VALUE_LIMITS)


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
      SELECT ch, day, ts, alarm, val_raw, val_num, obj, obj_parent, obj_kind,
             stype, sys, picket,
             epoch(ts) - lag(epoch(ts))    OVER (PARTITION BY ch, day ORDER BY ts) AS gap_s,
             epoch(ts) - lag(epoch(ts), 2) OVER (PARTITION BY ch, day ORDER BY ts) AS gap3_s,
             CASE WHEN val_raw IS DISTINCT FROM
                  lag(val_raw) OVER (PARTITION BY ch, day ORDER BY ts)
                  THEN 1 ELSE 0 END AS is_transition,
             -- Валидное числовое значение: аппаратные переполнения отсечены,
             -- иначе остаток и дрейф считаются по мусору.
             CASE WHEN val_num IS NULL THEN NULL
                  WHEN stype = 'Датчик температуры'
                       AND (val_num < -60 OR val_num > 150) THEN NULL
                  WHEN stype = 'Газовый датчик'
                       AND (val_num < 0 OR val_num >= {GAS_SATURATION}) THEN NULL
                  ELSE val_num END AS val_ok,
             -- Прогон одинаковых значений: залипший датчик отдаёт одно и то же
             -- подряд, и суточные min/max/mean/std этого не выражают.
             row_number() OVER (PARTITION BY ch, day ORDER BY ts)
           - row_number() OVER (PARTITION BY ch, day, val_raw ORDER BY ts)
             AS flat_grp,
             -- Сколько канал провисел в этом состоянии: до следующего события
             -- либо до конца суток. Обрезка концом суток обязательна — иначе
             -- признак пришлось бы ждать до закрытия тревоги, то есть смотреть
             -- в будущее. Деградация и износ по определению есть удлинение
             -- времени в ненормальном состоянии, а в панели не было ни одного
             -- признака длительности.
             coalesce(lead(epoch(ts)) OVER (PARTITION BY ch, day ORDER BY ts),
                      epoch(CAST(day AS TIMESTAMP) + INTERVAL 1 DAY))
             - epoch(ts) AS hold_s
      FROM {source}
    ), bins AS (
      -- Максимум событий в 10-минутном окне суток: флуд по EEMUA-191
      -- определяется именно на таком окне.
      SELECT ch, day, max(cnt) AS max_10min, max(alarm_cnt) AS max_alarm_10min,
             count(*) FILTER (WHERE alarm_cnt > 10) AS n_flood_bins
      FROM (
        SELECT ch, day, CAST(epoch(ts) AS BIGINT) // 600 AS b,
               count(*) AS cnt, count(*) FILTER (WHERE alarm) AS alarm_cnt
        FROM e GROUP BY ch, day, b
      ) GROUP BY ch, day
    ), chatter AS (
      -- Индекс дребезга по Kondaveeti (ChERD 2013): psi = sum(P_r / r), где
      -- r — интервал в секундах между соседними срабатываниями одного канала,
      -- P_r — доля таких интервалов за сутки. Подаётся непрерывным признаком:
      -- булев порог 0.05 доминируется членами r = 1..5 с и потому бесполезен.
      SELECT ch, day,
             sum(p_r / r) AS psi,
             sum(n_r) AS n_runs
      FROM (
        SELECT ch, day, r, n_r,
               CAST(n_r AS DOUBLE) / sum(n_r) OVER (PARTITION BY ch, day) AS p_r
        FROM (
          SELECT ch, day, CAST(gap_s AS BIGINT) AS r, count(*) AS n_r
          FROM e WHERE gap_s IS NOT NULL AND gap_s >= 1
          GROUP BY ch, day, CAST(gap_s AS BIGINT)
        )
      ) GROUP BY ch, day
    ), chatter_alarm AS (
      -- Тот же индекс, но только по тревожным событиям. Общий psi на числовых
      -- каналах (газ и температура дают 168 млн событий на ~500 каналов) меряет
      -- период опроса, а не дребезг, и через объектные агрегаты утекает в A' и C.
      SELECT ch, day, sum(p_r / r) AS psi_alarm
      FROM (
        SELECT ch, day, r, CAST(n_r AS DOUBLE) / sum(n_r) OVER (PARTITION BY ch, day) AS p_r
        FROM (
          SELECT ch, day, CAST(gap_s AS BIGINT) AS r, count(*) AS n_r
          FROM e WHERE alarm AND gap_s IS NOT NULL AND gap_s >= 1
          GROUP BY ch, day, CAST(gap_s AS BIGINT)
        )
      ) GROUP BY ch, day
    ), runs AS (
      -- Длина максимального прогона одинаковых значений за сутки. Считается
      -- отдельным шагом: это агрегат над агрегатом, в одном GROUP BY не
      -- выражается.
      SELECT ch, day, max(cnt) AS max_flat_run FROM (
        SELECT ch, day, flat_grp, count(*) AS cnt
        FROM e GROUP BY ch, day, flat_grp
      ) GROUP BY ch, day
    )
    SELECT ch, day,
           any_value(obj) AS obj, any_value(obj_parent) AS obj_parent,
           any_value(obj_kind) AS obj_kind, any_value(stype) AS stype,
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
           avg(val_num) AS val_mean, stddev_pop(val_num) AS val_std,
           min(val_ok) AS val_ok_min, max(val_ok) AS val_ok_max,
           avg(val_ok) AS val_ok_mean, median(val_ok) AS val_ok_med,
           count(val_ok) AS n_val_ok,
           count(*) FILTER (WHERE val_num IS NOT NULL AND val_ok IS NULL)
             AS n_val_bad,
           count(*) FILTER (WHERE val_num >= {GAS_SATURATION}) AS n_saturated,
           count(DISTINCT val_raw) AS n_distinct_vals,
           count(*) FILTER (WHERE val_ok > 0) AS n_val_nonzero,
           count(*) FILTER (WHERE val_ok > 0.05) AS n_val_gt005,
           count(*) FILTER (WHERE val_ok > 0.2) AS n_val_gt02,
           any_value(r.max_flat_run) AS max_flat_run,
           any_value(b.max_10min) AS max_10min,
           any_value(b.max_alarm_10min) AS max_alarm_10min,
           any_value(b.n_flood_bins) AS n_flood_bins,
           any_value(c.psi) AS chatter_psi,
           any_value(c.n_runs) AS chatter_runs,
           -- Час события несёт сигнал, которого нет больше нигде: тревоги
           -- концентрируются в рабочие часы (42 тыс. в 11:00 против 5-8 тыс.
           -- ночью), то есть отражают плановые работы, а не отказы. Это
           -- ближайший доступный заменитель отсутствующего АРМ-Контроля.
           max(CASE WHEN alarm THEN hour(ts) END) AS last_alarm_hour,
           CAST(count(*) FILTER (WHERE hour(ts) < 6) AS DOUBLE) / count(*)
             AS night_frac,
           CAST(count(*) FILTER (WHERE hour(ts) BETWEEN 9 AND 17) AS DOUBLE)
             / count(*) AS workhours_frac,
           CAST(count(*) FILTER (WHERE alarm AND hour(ts) < 6) AS DOUBLE)
             / greatest(count(*) FILTER (WHERE alarm), 1) AS night_alarm_frac,
           count(DISTINCT hour(ts)) AS n_active_hours,
           any_value(ca.psi_alarm) AS chatter_psi_alarm,
           -- Время в состоянии, накопленное к концу суток.
           CAST(coalesce(sum(hold_s) FILTER (WHERE alarm), 0) AS BIGINT) AS time_in_alarm_s,
           CAST(coalesce(sum(hold_s) FILTER (WHERE val_raw IN {_in(BAD_STATES)}), 0) AS BIGINT)
             AS time_in_bad_s,
           CAST(coalesce(max(hold_s) FILTER (WHERE alarm), 0) AS BIGINT) AS max_hold_alarm_s,
           median(hold_s) FILTER (WHERE alarm) AS med_hold_alarm_s,
           count(*) FILTER (WHERE alarm AND hold_s >= 4 * 3600)  AS n_standing_4h,
           count(*) FILTER (WHERE alarm AND hold_s >= 24 * 3600) AS n_stale_24h,
           -- Состояния, лежавшие в данных без употребления. «Много неисправных
           -- устройств» — диагноз, который система ставит себе сама.
           count(*) FILTER (WHERE val_raw = 'Много неисправных устройств') AS n_many_bad,
           count(*) FILTER (WHERE val_raw = 'Устройства на объекте исправны') AS n_devices_ok,
           count(*) FILTER (WHERE val_raw = 'Питание от батарей')           AS n_battery_power,
           count(*) FILTER (WHERE val_raw = 'Разговор')                     AS n_talk,
           count(*) FILTER (WHERE val_raw = 'Вызов')                        AS n_call,
           -- Охрана. Тревога проникновения при снятой охране — это проход
           -- персонала, а не нарушитель: по всей истории состояние известно
           -- у 86.7% тревог и делит их почти пополам (52.2% при охране).
           count(*) FILTER (WHERE val_raw = 'На охране')      AS n_arm,
           count(*) FILTER (WHERE val_raw = 'Снято с охраны') AS n_disarm,
           max(ts) FILTER (WHERE val_raw IN ('На охране', 'Снято с охраны')) AS last_arm_ts,
           arg_max(CAST(val_raw = 'На охране' AS INTEGER), ts)
             FILTER (WHERE val_raw IN ('На охране', 'Снято с охраны')) AS armed_eod
    FROM e JOIN runs r USING (ch, day)
           JOIN bins b USING (ch, day)
           LEFT JOIN chatter c USING (ch, day)
           LEFT JOIN chatter_alarm ca USING (ch, day)
    GROUP BY ch, day
    """)
