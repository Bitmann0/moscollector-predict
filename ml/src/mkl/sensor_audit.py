"""Аудит семантики датчиков и кандидатов в дневные цели.

Перенос аудита PR #7 (ветка analysis/deep-data-model-audit, модули
backend/ml/sensor_audit.py и target_audit.py) с хранилища normalized.duckdb на
представления mkl: `ev` — события из events_year=*.parquet, `daily_channel` —
суточная панель. Ничего не обучает и артефактов продукта не трогает, выдаёт
только агрегаты по типам датчиков: идентификаторов каналов в отчёте нет.

Ключи отчёта названы так же, как в PR #7 (sensor_type, sensor_value,
observed_channel_days и т. д.), а не как колонки mkl: так новый прогон
сверяется с analysis/experiments/deep-data-audit/sensor-audit.json и
target-audit.json построчно, функцией compare_with_reference.
"""
from __future__ import annotations

import datetime as dt

from .config import VALUE_LIMITS

UNMAPPED = "__UNMAPPED__"

# Категориальные типы для секундного аудита — те же 16, что в PR #7. Датчик
# движения исключён там же: он пишет десятки миллионов событий, а вопрос аудита —
# приходит ли «Неисправен» отдельным состоянием или в пакете с другими.
CATEGORICAL_SECOND_AUDIT_TYPES = (
    "9-секционный люк", "Датчик дыма", "Датчик затопления", "ИБП", "КД АВ",
    "КД Дверь", "КД Люк", "Переключатель", "Ручной извещатель",
    "Состояние УИР-Р", "Состояние вентилятора", "Состояние насоса",
    "Состояние охраны", "Состояние фазы", "Стекло", "Тепловой датчик",
)

_TEMP_LOW, _TEMP_HIGH = VALUE_LIMITS["Датчик температуры"]

# Кандидат в цель: тип датчика и условие на событие. Условия PR #7 без
# изменений, кроме числа: там было try_cast(replace(value, ',', '.')), здесь
# val_num из ingest. Суммарно это одни и те же 224 613 065 числовых событий
# (ml/reports/data_quality.md и sensor-audit.json PR #7).
#
# temperature_outside_3_40 оставлен по сырому val_num ради сверки с PR #7:
# переполнения -3276 и 999 (config.VALUE_LIMITS) в него попадают как «выход за
# диапазон». Вариант _valid отсекает их тем же пределом, что и суточная панель.
CANDIDATES = {
    "door_open_signal": ("КД Дверь", "val_raw = 'Не замкнут'"),
    "fan_fault_signal": ("Состояние вентилятора", "val_raw = 'Неисправен'"),
    "gas_alarm_flag": ("Газовый датчик", "alarm"),
    "phase_fault_signal": ("Состояние фазы", "val_raw = 'Неисправен'"),
    "pump_fault_signal": ("Состояние насоса", "val_raw = 'Неисправен'"),
    "smoke_detected_signal": ("Датчик дыма", "val_raw = 'Обнаружен дым'"),
    "smoke_fault_signal": ("Датчик дыма", "val_raw = 'Неисправен'"),
    "temperature_outside_3_40": ("Датчик температуры",
                                 "(val_num < 3 OR val_num > 40)"),
    "temperature_outside_3_40_valid": (
        "Датчик температуры",
        f"val_num BETWEEN {_TEMP_LOW} AND {_TEMP_HIGH}"
        " AND (val_num < 3 OR val_num > 40)"),
    "ups_battery_fault_signal": (
        "ИБП", "val_raw IN ('Батарея неисправна', 'Батарея разряжена')"),
    "ups_on_battery_signal": ("ИБП", "val_raw = 'Питание от батарей'"),
}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _in(values) -> str:
    return "(" + ", ".join(_quote(v) for v in sorted(set(values))) + ")"


def _rows(con, sql: str, params=None) -> list[dict]:
    return con.execute(sql, params or []).pl().to_dicts()


def sensor_overview(con, span_start: dt.date = dt.date(2024, 1, 7),
                    span_end: dt.date = dt.date(2026, 6, 23)) -> list[dict]:
    """Покрытие, объём, тревоги, числовая доля и непрерывность по типам.

    Покрытие и непрерывность считаются по суточной панели, кардинальность
    значений — по событиям. Отсутствующая строка панели — это не здоровые
    сутки, а сутки без наблюдений. channels_spanning_model_period — каналы,
    чьи первое и последнее наблюдение накрывают [span_start, span_end]; дыры
    внутри не исключены. Даты по умолчанию — из PR #7.
    """
    return _rows(con, f"""
      WITH panel AS (
        SELECT coalesce(stype, '{UNMAPPED}') AS sensor_type, ch, day,
               n_events, n_alarms
        FROM daily_channel
      ), overview AS (
        SELECT sensor_type,
               count(DISTINCT ch) AS observed_channels,
               count(*) AS observed_channel_days,
               count(DISTINCT day) AS observed_dates,
               CAST(sum(n_events) AS BIGINT) AS events,
               CAST(sum(n_alarms) AS BIGINT) AS alarms,
               min(day) AS first_day, max(day) AS last_day
        FROM panel GROUP BY sensor_type
      ), cardinality AS (
        SELECT coalesce(stype, '{UNMAPPED}') AS sensor_type,
               count(DISTINCT val_raw) AS distinct_values,
               count(*) FILTER (WHERE val_num IS NOT NULL) AS numeric_events,
               count(*) AS non_null_events
        FROM ev WHERE val_raw IS NOT NULL GROUP BY 1
      ), spans AS (
        SELECT sensor_type, ch, min(day) AS first_day, max(day) AS last_day,
               count(DISTINCT day) AS observed_days
        FROM panel GROUP BY sensor_type, ch
      ), continuity AS (
        SELECT sensor_type, count(*) AS channels,
               median(observed_days) AS median_observed_days,
               quantile_cont(observed_days, 0.1) AS p10_observed_days,
               quantile_cont(observed_days, 0.9) AS p90_observed_days,
               median(date_diff('day', first_day, last_day) + 1) AS median_span_days,
               count(*) FILTER (WHERE first_day <= ? AND last_day >= ?)
                 AS channels_spanning_model_period
        FROM spans GROUP BY sensor_type
      )
      SELECT o.*, c.distinct_values, c.numeric_events, c.non_null_events,
             s.channels, s.median_observed_days, s.p10_observed_days,
             s.p90_observed_days, s.median_span_days,
             s.channels_spanning_model_period,
             CAST(o.alarms AS DOUBLE) / nullif(o.events, 0) AS alarm_rate,
             CAST(c.numeric_events AS DOUBLE) / nullif(c.non_null_events, 0)
               AS numeric_event_fraction
      FROM overview o
      LEFT JOIN cardinality c USING (sensor_type)
      LEFT JOIN continuity s USING (sensor_type)
      ORDER BY o.events DESC, o.sensor_type
    """, [span_start, span_end])


def sensor_yearly(con) -> list[dict]:
    """Те же счётчики покрытия по календарным годам: видна смена парка."""
    return _rows(con, f"""
      SELECT coalesce(stype, '{UNMAPPED}') AS sensor_type,
             year(day) AS calendar_year,
             count(DISTINCT ch) AS observed_channels,
             count(*) AS observed_channel_days,
             count(DISTINCT day) AS observed_dates,
             CAST(sum(n_events) AS BIGINT) AS events,
             CAST(sum(n_alarms) AS BIGINT) AS alarms
      FROM daily_channel GROUP BY 1, 2 ORDER BY 1, 2
    """)


def value_top(con, top_n: int = 30) -> list[dict]:
    """Самые частые значения каждого типа с числом тревожных среди них.

    По этой таблице видно, что флаг alarm означает разное у разных типов:
    один словарь «тревожных значений» на все типы не годится.
    """
    return _rows(con, f"""
      WITH grouped AS (
        SELECT coalesce(stype, '{UNMAPPED}') AS sensor_type,
               val_raw AS sensor_value, count(*) AS events,
               count(DISTINCT ch) AS channels,
               CAST(sum(CAST(alarm AS BIGINT)) AS BIGINT) AS alarms,
               min(day) AS first_day, max(day) AS last_day
        FROM ev WHERE val_raw IS NOT NULL GROUP BY 1, 2
      ), ranked AS (
        SELECT *, row_number() OVER (PARTITION BY sensor_type
                                     ORDER BY events DESC, sensor_value)
                  AS value_rank
        FROM grouped
      )
      SELECT * FROM ranked WHERE value_rank <= ?
      ORDER BY sensor_type, value_rank
    """, [top_n])


def same_second_states(con, start: dt.date, end: dt.date,
                       types=CATEGORICAL_SECOND_AUDIT_TYPES) -> list[dict]:
    """Сколько секунд канала несут несколько разных значений сразу.

    Порядок записей внутри одной секунды в журнале не определён, поэтому
    секунда здесь — неупорядоченный набор значений. mixed_fault_seconds —
    секунды, где «Неисправен» пришёл вместе с другим значением: такой
    «Неисправен» нельзя читать как самостоятельное начало отказа.
    """
    return _rows(con, f"""
      WITH seconds AS (
        SELECT stype AS sensor_type, ch, ts,
               count(DISTINCT val_raw) AS states,
               max(CAST(alarm AS INTEGER)) AS alarm,
               max(CAST(val_raw = 'Неисправен' AS INTEGER)) AS fault
        FROM ev
        WHERE stype IN {_in(types)} AND day BETWEEN ? AND ?
          AND val_raw IS NOT NULL
        GROUP BY 1, 2, 3
      )
      SELECT sensor_type, count(*) AS observed_seconds,
             count(*) FILTER (WHERE states > 1) AS mixed_state_seconds,
             count(*) FILTER (WHERE alarm = 1) AS alarm_seconds,
             count(*) FILTER (WHERE alarm = 1 AND states > 1) AS mixed_alarm_seconds,
             count(*) FILTER (WHERE fault = 1) AS fault_seconds,
             count(*) FILTER (WHERE fault = 1 AND states > 1) AS mixed_fault_seconds,
             max(states) AS maximum_states_in_one_second
      FROM seconds GROUP BY sensor_type
      ORDER BY observed_seconds DESC, sensor_type
    """, [start, end])


def target_candidates(con, candidates: dict | None = None) -> dict:
    """Кандидаты в суточную цель: доля, повтор, начало после чистого дня, дрейф.

    Целевой канал-день — сутки, в которые канал хотя бы раз записал условие
    кандидата. Наблюдаемый канал-день — строка суточной панели для канала
    нужного типа. starts_after_observed_non_target_day — целевые сутки, перед
    которыми был наблюдаемый день без цели; день без телеметрии нормой не
    считается. target_days_outside_panel должен быть нулём: иначе события и
    панель собраны из разных версий журнала.
    """
    candidates = candidates or CANDIDATES
    flags = ",\n".join(
        f'count(*) FILTER (WHERE stype = {_quote(st)} AND ({cond})) AS "{name}"'
        for name, (st, cond) in candidates.items())
    # Один проход по событиям вместо прохода на каждого кандидата: газ и
    # температура — это 224 млн числовых событий.
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE _candidate_flags AS
      SELECT ch, day, {flags}
      FROM ev WHERE stype IN {_in(st for st, _ in candidates.values())}
      GROUP BY ch, day
    """)
    con.execute("CREATE OR REPLACE TEMP TABLE _target_days AS " + " UNION ALL ".join(
        f"SELECT {_quote(name)} AS candidate, ch, day, \"{name}\" AS target_events "
        f"FROM _candidate_flags WHERE \"{name}\" > 0" for name in candidates))
    types = " UNION ALL ".join(f"SELECT {_quote(name)} AS candidate, {_quote(st)} AS stype"
                               for name, (st, _) in candidates.items())
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE _observed_days AS
      SELECT DISTINCT t.candidate, d.ch, d.day
      FROM daily_channel d JOIN ({types}) t ON t.stype = d.stype
    """)
    summary = _rows(con, """
      WITH observed AS (
        SELECT candidate, count(*) AS observed_channel_days,
               count(DISTINCT ch) AS observed_channels
        FROM _observed_days GROUP BY candidate
      ), target AS (
        SELECT candidate, count(*) AS target_channel_days,
               CAST(sum(target_events) AS BIGINT) AS target_events,
               count(DISTINCT ch) AS target_channels,
               min(day) AS first_target_day, max(day) AS last_target_day
        FROM _target_days GROUP BY candidate
      ), transitions AS (
        SELECT t.candidate,
               count(*) FILTER (WHERE nt.ch IS NOT NULL) AS repeated_next_day,
               count(*) FILTER (WHERE po.ch IS NOT NULL AND pt.ch IS NULL)
                 AS starts_after_observed_non_target_day,
               count(*) FILTER (WHERE od.ch IS NULL) AS target_days_outside_panel
        FROM _target_days t
        LEFT JOIN _target_days nt ON nt.candidate = t.candidate AND nt.ch = t.ch
             AND nt.day = t.day + 1
        LEFT JOIN _observed_days po ON po.candidate = t.candidate AND po.ch = t.ch
             AND po.day = t.day - 1
        LEFT JOIN _target_days pt ON pt.candidate = t.candidate AND pt.ch = t.ch
             AND pt.day = t.day - 1
        LEFT JOIN _observed_days od ON od.candidate = t.candidate AND od.ch = t.ch
             AND od.day = t.day
        GROUP BY t.candidate
      )
      SELECT o.candidate, o.observed_channels, o.observed_channel_days,
             coalesce(t.target_channels, 0) AS target_channels,
             coalesce(t.target_channel_days, 0) AS target_channel_days,
             coalesce(t.target_events, 0) AS target_events,
             t.first_target_day, t.last_target_day,
             coalesce(x.repeated_next_day, 0) AS repeated_next_day,
             coalesce(x.starts_after_observed_non_target_day, 0)
               AS starts_after_observed_non_target_day,
             coalesce(x.target_days_outside_panel, 0) AS target_days_outside_panel,
             CAST(coalesce(t.target_channel_days, 0) AS DOUBLE)
               / o.observed_channel_days AS target_day_rate
      FROM observed o LEFT JOIN target t USING (candidate)
      LEFT JOIN transitions x USING (candidate)
      ORDER BY target_day_rate DESC, o.candidate
    """)
    yearly = _rows(con, """
      SELECT o.candidate, year(o.day) AS calendar_year,
             count(*) AS observed_channel_days,
             count(t.ch) AS target_channel_days,
             count(DISTINCT t.ch) AS target_channels
      FROM _observed_days o
      LEFT JOIN _target_days t USING (candidate, ch, day)
      GROUP BY 1, 2 ORDER BY 1, 2
    """)
    concentration = _rows(con, """
      WITH counts AS (
        SELECT candidate, ch, count(*) AS target_days
        FROM _target_days GROUP BY 1, 2
      ), ranked AS (
        SELECT *, row_number() OVER (PARTITION BY candidate
                                     ORDER BY target_days DESC, ch) AS channel_rank,
               sum(target_days) OVER (PARTITION BY candidate) AS total_days
        FROM counts
      )
      SELECT candidate, CAST(max(total_days) AS BIGINT) AS target_days,
             sum(target_days) FILTER (WHERE channel_rank <= 1) / max(total_days)
               AS top_1_share,
             sum(target_days) FILTER (WHERE channel_rank <= 5) / max(total_days)
               AS top_5_share,
             sum(target_days) FILTER (WHERE channel_rank <= 10) / max(total_days)
               AS top_10_share
      FROM ranked GROUP BY 1 ORDER BY 1
    """)
    return {"candidates": summary, "yearly": yearly,
            "channel_concentration": concentration,
            "target_definitions": {name: {"sensor_type": st, "event_condition": cond}
                                   for name, (st, cond) in candidates.items()}}


def audit_report(con, *, second_start: dt.date, second_end: dt.date,
                 span_start: dt.date = dt.date(2024, 1, 7),
                 span_end: dt.date = dt.date(2026, 6, 23),
                 top_n: int = 30) -> dict:
    """Весь аудит одним словарём; происхождение данных добавляет скрипт."""
    return {
        "sensor_types": sensor_overview(con, span_start, span_end),
        "yearly": sensor_yearly(con),
        "top_values": value_top(con, top_n),
        "same_second_state_audit": {
            "start": second_start, "end": second_end,
            "types": list(CATEGORICAL_SECOND_AUDIT_TYPES),
            "rows": same_second_states(con, second_start, second_end)},
        "targets": target_candidates(con),
    }


# Поля, по которым новый прогон сверяется с отчётами PR #7. Доли не сверяются:
# они выводятся из этих счётчиков.
_PARITY = {
    "sensor_types": ("sensor_type", ("observed_channels", "observed_channel_days",
                                     "events", "alarms", "distinct_values",
                                     "numeric_events", "non_null_events",
                                     "channels_spanning_model_period")),
    "same_second": ("sensor_type", ("observed_seconds", "mixed_state_seconds",
                                    "alarm_seconds", "mixed_alarm_seconds",
                                    "fault_seconds", "mixed_fault_seconds",
                                    "maximum_states_in_one_second")),
    "candidates": ("candidate", ("observed_channels", "observed_channel_days",
                                 "target_channels", "target_channel_days",
                                 "target_events", "repeated_next_day",
                                 "starts_after_observed_non_target_day")),
}


def compare_with_reference(report: dict, sensor_ref: dict, target_ref: dict) -> dict:
    """Расхождения счётчиков с отчётами PR #7 по ключу строки.

    В JSON PR #7 часть целых записана как 9439655.0, поэтому сравнение
    числовое. Строка, которой нет с одной из сторон, — тоже расхождение: у
    кандидата temperature_outside_3_40_valid пары в PR #7 нет, он пропускается.
    """
    pairs = {
        "sensor_types": (report["sensor_types"], sensor_ref["sensor_types"]),
        "same_second": (report["same_second_state_audit"]["rows"],
                        sensor_ref["same_second_state_audit_2024_2026"]),
        "candidates": ([r for r in report["targets"]["candidates"]
                        if r["candidate"] != "temperature_outside_3_40_valid"],
                       target_ref["candidates"]),
    }
    mismatches, checked = [], 0
    for section, (ours, theirs) in pairs.items():
        key, fields = _PARITY[section]
        ours_by, theirs_by = ({r[key]: r for r in ours}, {r[key]: r for r in theirs})
        for name in sorted(set(ours_by) | set(theirs_by)):
            a, b = ours_by.get(name), theirs_by.get(name)
            if a is None or b is None:
                mismatches.append({"section": section, key: name,
                                   "missing_in": "new" if a is None else "reference"})
                continue
            for field in fields:
                checked += 1
                if a.get(field) is None or b.get(field) is None:
                    if a.get(field) != b.get(field):
                        mismatches.append({"section": section, key: name, "field": field,
                                           "new": a.get(field), "reference": b.get(field)})
                elif float(a[field]) != float(b[field]):
                    mismatches.append({"section": section, key: name, "field": field,
                                       "new": a[field], "reference": b[field],
                                       "delta": float(a[field]) - float(b[field])})
    return {"checked_values": checked, "mismatches": mismatches}
