"""Почасовые признаки температурных каналов и метка «выход за [3, 40] °C завтра».

Перенос из PR #8 (ветка feature/episode-hourly-backtest, коммит 7aa50f9). Там
признаки строились SQL-запросом к DuckDB-хранилищу PR #4, которого в mkl нет;
здесь источник — представление ev над data/interim/events_year=*.parquet
(db.attach_events). Суточная панель daily_channel для этой постановки не годится:
окон 6, 24 и 72 часа в ней нет.

Метка — первое числовое показание вне [3, 40] °C в ближайшие 24 часа после 24
или 72 часов без такого показания. Это прокси температурного эпизода, а не
подтверждённая неисправность: по аудиту PR 95% начал эпизодов состоят из ровно
0, 1 или 2 °C, и ни одна запись вне диапазона не помечена тревожной
(reports/temperature_episode_target_audit.json).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .config import EXCLUDED_PERIODS, VALUE_LIMITS

STYPE = "Датчик температуры"
WINDOW_HOURS = (6, 24, 72, 168, 720)

# Границы взяты из текстовых состояний самого датчика: «Температура ниже 3ºC» и
# «Температура выше 40ºC» у типа «Датчик температуры» (analysis/annual_profiles.json,
# например строка 2779; второе состояние есть и в FIRE_STATES, config.py:40).
# Владелец данных их не подтверждал — это вывод из журнала.
LOW_C = 3.0
HIGH_C = 40.0

# Диапазон правдоподобных значений общий с остальным mkl: -3276 и 999 —
# аппаратные переполнения, а не температура (config.VALUE_LIMITS).
VALID_LOW_C, VALID_HIGH_C = VALUE_LIMITS[STYPE]

FEATURES_FILE = "temperature_episode_hourly.parquet"


def _excluded_sql(col: str) -> str:
    """Условие «вне исключённых периодов». Даты config.EXCLUDED_PERIODS включительные."""
    parts = [f"CAST({col} AS DATE) BETWEEN DATE '{a}' AND DATE '{b}'"
             for a, b in EXCLUDED_PERIODS]
    return "NOT (" + " OR ".join(parts) + ")" if parts else "TRUE"


def _valid(col: str = "parsed_value") -> str:
    return f"{col} BETWEEN {VALID_LOW_C} AND {VALID_HIGH_C}"


def _out_of_range(col: str = "parsed_value") -> str:
    return f"{_valid(col)} AND ({col} < {LOW_C} OR {col} > {HIGH_C})"


def load_seconds(con: duckdb.DuckDBPyConnection, source: str = "ev") -> None:
    """Временные таблицы temp_raw (записи) и temp_seconds (канал-секунда).

    Дедупликация — по содержанию (канал, момент, тревожность, значение), без
    event_id: так считал PR («channel,timestamp,alarm,value»), и с event_id в
    DISTINCT raw_rows разошлись бы с reports/temperature_episode_sources.json.

    Значение разбирается заново из val_raw с заменой запятой на точку. Колонка
    val_num для этого не годится: ingest делает TRY_CAST без замены, и «20,5»
    там NULL.

    ts в ev уже местное время без зоны, поэтому пересчёт `AT TIME ZONE
    'Europe/Moscow'` из PR не нужен.
    """
    stype = STYPE.replace("'", "''")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE temp_raw AS
        SELECT DISTINCT ch AS channel, ts, alarm,
            val_raw AS sensor_value,
            try_cast(replace(val_raw, ',', '.') AS DOUBLE) AS parsed_value
        FROM {source}
        WHERE stype = '{stype}' AND ch IS NOT NULL AND ts IS NOT NULL
          AND {_excluded_sql('ts')}
    """)
    # В одной секунде у канала бывает несколько разных значений, и порядка между
    # ними источник не даёт. Секунда сворачивается как неупорядоченное множество:
    # min, max, mean и флаг «есть значение вне диапазона».
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE temp_seconds AS
        SELECT channel, ts,
            count(*) AS records,
            count(*) FILTER (WHERE {_valid()}) AS numeric_records,
            min(parsed_value) FILTER (WHERE {_valid()}) AS value_min,
            max(parsed_value) FILTER (WHERE {_valid()}) AS value_max,
            avg(parsed_value) FILTER (WHERE {_valid()}) AS value_mean,
            coalesce(max(CAST({_out_of_range()} AS INTEGER)), 0) AS bad
        FROM temp_raw
        GROUP BY channel, ts
    """)


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


def _window_sql(hours: int) -> str:
    quantiles = ""
    if hours == 720:
        quantiles = """,
            quantile_cont(s.value_mean, 0.25) FILTER (WHERE s.value_mean IS NOT NULL)
                AS value_q25_720h,
            quantile_cont(s.value_mean, 0.50) FILTER (WHERE s.value_mean IS NOT NULL)
                AS value_median_720h,
            quantile_cont(s.value_mean, 0.75) FILTER (WHERE s.value_mean IS NOT NULL)
                AS value_q75_720h"""
    # Окно [as_of - hours, as_of): запись в момент as_of уже относится к метке.
    return f"""
        CREATE OR REPLACE TEMP TABLE feature_{hours}h AS
        SELECT g.channel, g.as_of,
            count(s.ts) AS observed_seconds_{hours}h,
            coalesce(sum(s.records), 0) AS events_{hours}h,
            coalesce(sum(s.numeric_records), 0) AS numeric_records_{hours}h,
            coalesce(sum(s.bad), 0) AS bad_seconds_{hours}h,
            min(s.value_min) AS value_min_{hours}h,
            max(s.value_max) AS value_max_{hours}h,
            avg(s.value_mean) AS value_mean_{hours}h,
            stddev_pop(s.value_mean) AS value_std_{hours}h,
            arg_min(s.value_mean, s.ts) FILTER (WHERE s.value_mean IS NOT NULL)
                AS value_first_{hours}h,
            arg_max(s.value_mean, s.ts) FILTER (WHERE s.value_mean IS NOT NULL)
                AS value_last_{hours}h,
            regr_slope(s.value_mean, epoch(s.ts)) * 3600 AS value_slope_per_hour_{hours}h,
            count(DISTINCT s.value_mean) FILTER (WHERE s.value_mean IS NOT NULL)
                AS distinct_values_{hours}h,
            date_diff('minute', max(s.ts) FILTER (WHERE s.value_mean IS NOT NULL), g.as_of)
                / 60.0 AS hours_since_value_{hours}h
            {quantiles}
        FROM grid g
        LEFT JOIN temp_seconds s
          ON s.channel = g.channel
         AND s.ts >= g.as_of - INTERVAL {hours} HOUR
         AND s.ts < g.as_of
        GROUP BY g.channel, g.as_of
    """


def sha256_file(path: Path, chunk: int = 1 << 24) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def build_features(con: duckdb.DuckDBPyConnection, out_path: Path, source: str = "ev",
                   raw_dir: Path | None = None,
                   catalog_path: Path | None = None) -> dict:
    """Строка на канал и сутки прогноза: признаки только по прошлому и будущее окно.

    Соединение создаёт вызывающий (db.connect и db.attach_events), чтобы тест
    мог подставить таблицу ev из фикстуры. Parquet пишется прямым COPY, мимо
    store.write: реестр configs/features.yaml уходит в бандл, а этой постановке
    там не место.

    raw_dir и catalog_path нужны только для манифеста — хеши исходных файлов.
    По умолчанию не хешируется ничего: восемь годовых CSV весят гигабайты, и
    тесту это не нужно.
    """
    load_seconds(con, source)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE bounds AS
        SELECT channel,
            date_trunc('day', min(ts)) + INTERVAL 1 DAY AS first_day,
            date_trunc('day', max(ts)) AS last_day
        FROM temp_seconds
        GROUP BY channel
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE grid AS
        SELECT channel, generate_series AS as_of
        FROM bounds,
        generate_series(first_day, last_day, INTERVAL 1 DAY)
    """)
    for hours in WINDOW_HOURS:
        con.execute(_window_sql(hours))
    con.execute("""
        CREATE OR REPLACE TEMP TABLE future AS
        SELECT g.channel, g.as_of,
            count(s.ts) AS future_observed_seconds,
            coalesce(sum(s.numeric_records), 0) AS future_numeric_records,
            coalesce(sum(s.bad), 0) AS future_bad_seconds
        FROM grid g
        LEFT JOIN temp_seconds s
          ON s.channel = g.channel
         AND s.ts >= g.as_of
         AND s.ts < g.as_of + INTERVAL 24 HOUR
        GROUP BY g.channel, g.as_of
    """)
    joins = "\n".join(
        f"JOIN feature_{hours}h USING (channel, as_of)" for hours in WINDOW_HOURS
    )
    select_columns = ["g.channel", "g.as_of"]
    for hours in WINDOW_HOURS:
        select_columns.append(f"feature_{hours}h.* EXCLUDE (channel, as_of)")
    select_columns.extend([
        "future.future_observed_seconds",
        "future.future_numeric_records",
        "future.future_bad_seconds",
    ])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(out_path.resolve()).replace("'", "''")
    con.execute(f"""
        COPY (
            SELECT {', '.join(select_columns)}
            FROM grid g
            {joins}
            JOIN future USING (channel, as_of)
            ORDER BY as_of, channel
        ) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    raw_stats = con.execute(f"""
        SELECT count(*), count(DISTINCT channel),
            count(*) FILTER (WHERE parsed_value IS NOT NULL),
            count(*) FILTER (WHERE {_valid()})
        FROM temp_raw
    """).fetchone()
    feature_stats = con.execute(
        f"SELECT count(*), min(as_of), max(as_of) FROM read_parquet('{escaped}')"
    ).fetchone()

    # Таблицы ingest_sources из PR #4 в mkl нет: хеши считаются по самим файлам.
    sources = []
    if raw_dir is not None and Path(raw_dir).is_dir():
        sources = [{"filename": p.name, "sha256": sha256_file(p),
                    "time_zone": "Europe/Moscow"}
                   for p in sorted(Path(raw_dir).glob("ext-journal-*.csv"))]
    catalog_sha = (sha256_file(Path(catalog_path))
                   if catalog_path is not None and Path(catalog_path).is_file() else None)
    return {
        "target": (
            "First numeric temperature outside [3,40] C in the next calendar day "
            "after a fully clean lookback"
        ),
        "timezone": "Europe/Moscow",
        "semantic_deduplication": "channel,timestamp,alarm,value",
        "same_second_policy": "unordered set aggregated with min/max/mean",
        "valid_numeric_range_c": [VALID_LOW_C, VALID_HIGH_C],
        "target_range_c": [LOW_C, HIGH_C],
        "feature_windows_hours": list(WINDOW_HOURS),
        # В PR был полуоткрытый ["2021-04-01", "2021-07-01"); здесь те же сутки
        # записаны включительно, как в config.EXCLUDED_PERIODS.
        "excluded_periods_inclusive": [[str(a), str(b)] for a, b in EXCLUDED_PERIODS],
        "raw_rows": int(raw_stats[0]),
        "channels": int(raw_stats[1]),
        "parsed_rows": int(raw_stats[2]),
        "valid_numeric_rows": int(raw_stats[3]),
        "feature_rows": int(feature_stats[0]),
        "as_of_min": str(feature_stats[1]),
        "as_of_max": str(feature_stats[2]),
        "catalog_sha256": catalog_sha,
        "sources": sources,
        "limitations": [
            "The [3,40] C range is inferred from sensor text and is not confirmed by the owner.",
            "An out-of-range measurement is a proxy episode, not a confirmed equipment failure.",
            "The current catalog is applied to history without historical equipment versions.",
        ],
    }


def channel_bucket(channel: int) -> int:
    """Детерминированная корзина 0..4: корзина 0 — каналы, скрытые от обучения."""
    digest = hashlib.sha256(str(int(channel)).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 5


def feature_columns() -> list[str]:
    columns: list[str] = []
    for hours in WINDOW_HOURS:
        columns.extend([
            f"observed_seconds_{hours}h",
            f"events_{hours}h",
            f"numeric_records_{hours}h",
            f"value_min_{hours}h",
            f"value_max_{hours}h",
            f"value_mean_{hours}h",
            f"value_std_{hours}h",
            f"value_first_{hours}h",
            f"value_last_{hours}h",
            f"value_slope_per_hour_{hours}h",
            f"distinct_values_{hours}h",
            f"hours_since_value_{hours}h",
            f"numeric_fraction_{hours}h",
            f"value_change_{hours}h",
            f"value_range_{hours}h",
        ])
    columns.extend([
        "distance_to_low_24h",
        "distance_to_high_24h",
        "sampling_ratio_24_to_168h",
        "value_iqr_720h",
        "value_residual_iqr_720h",
        "weekday_sin",
        "weekday_cos",
        "year_sin",
        "year_cos",
    ])
    return columns


def _in_excluded(as_of: pd.Series) -> pd.Series:
    mask = pd.Series(False, index=as_of.index)
    for a, b in EXCLUDED_PERIODS:
        mask |= as_of.ge(pd.Timestamp(a)) & as_of.lt(pd.Timestamp(b) + pd.Timedelta(days=1))
    return mask


def make_samples(frame: pd.DataFrame, clean_hours: int = 24) -> tuple[pd.DataFrame, dict]:
    """Выборка с меткой и производными признаками.

    Строка годится, если за последние 24 часа было числовое показание, окно
    clean_hours чистое и завтра телеметрия есть хотя бы на четверть недельного
    ритма. Последнее условие смотрит в будущее: канал, который завтра замолчит,
    из оценки выпадает. В сервисе такой алерт стал бы unknown, поэтому метрики
    отчёта условны по наличию завтрашних данных.
    """
    if clean_hours not in (24, 72):
        raise ValueError("clean_hours must be 24 or 72")
    data = frame.copy()
    data["as_of"] = pd.to_datetime(data["as_of"])
    expected_future = (data["numeric_records_168h"] / 7).clip(lower=1)
    data["future_cadence_ratio"] = data["future_numeric_records"] / expected_future
    eligibility = (
        data["numeric_records_24h"].gt(0)
        & data["future_numeric_records"].gt(0)
        & data["future_cadence_ratio"].ge(0.25)
        & data[f"bad_seconds_{clean_hours}h"].eq(0)
    )
    selected = data.loc[eligibility & ~_in_excluded(data.as_of)].copy()
    selected["target"] = selected.future_bad_seconds.gt(0).astype(int)
    selected["label_end"] = selected.as_of + pd.Timedelta(days=1)
    selected["channel_bucket"] = selected.channel.map(channel_bucket)
    for hours in WINDOW_HOURS:
        selected[f"numeric_fraction_{hours}h"] = (
            selected[f"numeric_records_{hours}h"]
            / selected[f"events_{hours}h"].replace(0, np.nan)
        )
        selected[f"value_change_{hours}h"] = (
            selected[f"value_last_{hours}h"] - selected[f"value_first_{hours}h"]
        )
        selected[f"value_range_{hours}h"] = (
            selected[f"value_max_{hours}h"] - selected[f"value_min_{hours}h"]
        )
    selected["distance_to_low_24h"] = selected.value_min_24h - LOW_C
    selected["distance_to_high_24h"] = HIGH_C - selected.value_max_24h
    selected["sampling_ratio_24_to_168h"] = (
        selected.numeric_records_24h / expected_future.loc[selected.index]
    )
    selected["value_iqr_720h"] = selected.value_q75_720h - selected.value_q25_720h
    selected["value_residual_iqr_720h"] = (
        (selected.value_last_24h - selected.value_median_720h)
        / selected.value_iqr_720h.replace(0, np.nan)
    )
    selected["weekday_sin"] = np.sin(2 * np.pi * selected.as_of.dt.dayofweek / 7)
    selected["weekday_cos"] = np.cos(2 * np.pi * selected.as_of.dt.dayofweek / 7)
    selected["year_sin"] = np.sin(2 * np.pi * selected.as_of.dt.dayofyear / 365.25)
    selected["year_cos"] = np.cos(2 * np.pi * selected.as_of.dt.dayofyear / 365.25)
    quality = {
        # Имя ключа из PR: это строки признаков (канало-сутки), а не записи журнала.
        "raw_rows": len(data),
        "eligible_rows": len(selected),
        "positives": int(selected.target.sum()),
        "excluded_unknown_or_low_future_cadence": int(
            (data.numeric_records_24h.gt(0) & ~(
                data.future_numeric_records.gt(0) & data.future_cadence_ratio.ge(0.25)
            )).sum()
        ),
        "clean_hours": clean_hours,
    }
    return selected, quality


def target_audit(con: duckdb.DuckDBPyConnection, samples_72h: pd.DataFrame | None = None,
                 source: str = "ev", onset_gap_hours: int = 72) -> dict:
    """Аудит метки: ключи reports/temperature_episode_target_audit.json.

    Генератора этих чисел в PR нет, поэтому определения восстановлены по их
    описанию в README PR, и совпадение с эталоном при перезапуске не гарантировано:
    - начало эпизода — плохая секунда, перед которой у канала не было плохой
      секунды дольше onset_gap_hours часов (строго больше: чистое окно в
      признаках — полуоткрытое [as_of - 72 ч, as_of));
    - high — в секунде начала есть значение выше HIGH_C; иначе exact 0/1/2 —
      все значения вне диапазона в этой секунде равны ровно 0, 1 или 2 °C;
      остальное — other_low. Три класса делят начала без остатка.

    samples_72h — выход make_samples(frame, 72). Без него счётчики «eligible»
    не считаются. Идентификаторы каналов в результат не попадают.
    """
    if not _has_table(con, "temp_raw"):
        load_seconds(con, source)
    records = con.execute(f"""
        SELECT count(*), count(DISTINCT channel), count(*) FILTER (WHERE alarm)
        FROM temp_raw WHERE {_out_of_range()}
    """).fetchone()
    seconds = con.execute(f"""
        WITH per_second AS (
            SELECT channel, ts,
                bool_or({_out_of_range()}) AS bad,
                bool_or(parsed_value BETWEEN {LOW_C} AND {HIGH_C}) AS has_in_range,
                bool_or({_out_of_range()} AND parsed_value > {HIGH_C}) AS has_high,
                bool_and(parsed_value IN (0, 1, 2)) FILTER (WHERE {_out_of_range()})
                    AS only_012
            FROM temp_raw
            GROUP BY channel, ts
        ), bad AS (
            SELECT *, lag(ts) OVER (PARTITION BY channel ORDER BY ts) AS prev_ts
            FROM per_second WHERE bad
        ), onsets AS (
            SELECT * FROM bad
            WHERE prev_ts IS NULL OR ts - prev_ts > INTERVAL {int(onset_gap_hours)} HOUR
        )
        SELECT
            (SELECT count(*) FROM bad),
            (SELECT count(*) FROM bad WHERE has_in_range),
            count(*),
            count(*) FILTER (WHERE NOT has_high AND only_012),
            count(*) FILTER (WHERE NOT has_high AND NOT only_012),
            count(*) FILTER (WHERE has_high)
        FROM onsets
    """).fetchone()
    onsets = int(seconds[2])
    result = {
        "valid_out_of_range_records": int(records[0]),
        "channels_with_out_of_range_records": int(records[1]),
        "alarm_true_records": int(records[2]),
        "bad_seconds": int(seconds[0]),
        "mixed_bad_and_in_range_seconds": int(seconds[1]),
        f"raw_onsets_separated_by_{onset_gap_hours}h": onsets,
        "onsets_exact_0_1_2_only": int(seconds[3]),
        "onsets_exact_0_1_2_share": int(seconds[3]) / onsets if onsets else 0.0,
        "other_low_onsets": int(seconds[4]),
        "high_onsets": int(seconds[5]),
    }
    if samples_72h is not None:
        positive = samples_72h.loc[samples_72h.target.eq(1)]
        per_channel = positive.channel.value_counts()
        total = len(positive)
        result.update({
            "eligible_72h_positive_channel_days": total,
            "eligible_72h_positive_channels": int(per_channel.size),
            "eligible_72h_top_10_channel_share": (
                float(per_channel.nlargest(10).sum() / total) if total else 0.0
            ),
        })
    result["notes"] = [
        "Counts exclude the organizer-confirmed April-June 2021 migration period.",
        ("Raw onset counts are event-time diagnostics and do not apply the "
         "future-cadence eligibility filter."),
        "Channel identifiers are intentionally omitted from the committed aggregate report.",
    ]
    return result


def write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
