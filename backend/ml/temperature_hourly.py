"""Build leakage-safe temperature episode features from the normalized warehouse."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd

WINDOW_HOURS = (6, 24, 72, 168, 720)
LOW_C = 3.0
HIGH_C = 40.0
VALID_LOW_C = -60.0
VALID_HIGH_C = 150.0
MIGRATION_START = "2021-04-01"
MIGRATION_END = "2021-07-01"


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


def prepare(database: Path, catalog_path: Path, output: Path) -> dict:
    """Create one row per channel and prediction day with past-only features."""
    catalog = pd.read_csv(catalog_path)
    selected_ids = catalog.loc[
        catalog["тип_датчика"].eq("Датчик температуры"), ["ид_канала_данных"]
    ].rename(columns={"ид_канала_данных": "channel"})
    if selected_ids.empty:
        raise ValueError("No temperature channels in catalog")

    output.mkdir(parents=True, exist_ok=True)
    parquet_path = output / "temperature_episode_features.parquet"
    escaped_output = str(parquet_path.resolve()).replace("'", "''")
    with duckdb.connect(str(database), read_only=True) as con:
        con.execute("SET memory_limit='4GB'")
        con.execute("SET threads=4")
        con.register("selected_ids", selected_ids)
        con.execute(f"""
            CREATE TEMP TABLE temp_raw AS
            SELECT DISTINCT channel_id AS channel,
                event_time AT TIME ZONE 'Europe/Moscow' AS ts,
                alarm,
                sensor_value,
                try_cast(replace(sensor_value, ',', '.') AS DOUBLE) AS parsed_value
            FROM events
            JOIN selected_ids ON channel_id = selected_ids.channel
            WHERE event_time IS NOT NULL
              AND NOT (
                event_time >= TIMESTAMPTZ '{MIGRATION_START} 00:00:00 Europe/Moscow'
                AND event_time < TIMESTAMPTZ '{MIGRATION_END} 00:00:00 Europe/Moscow'
              )
        """)
        con.execute(f"""
            CREATE TEMP TABLE temp_seconds AS
            SELECT channel, ts,
                count(*) AS records,
                count(*) FILTER (
                    WHERE parsed_value BETWEEN {VALID_LOW_C} AND {VALID_HIGH_C}
                ) AS numeric_records,
                min(parsed_value) FILTER (
                    WHERE parsed_value BETWEEN {VALID_LOW_C} AND {VALID_HIGH_C}
                ) AS value_min,
                max(parsed_value) FILTER (
                    WHERE parsed_value BETWEEN {VALID_LOW_C} AND {VALID_HIGH_C}
                ) AS value_max,
                avg(parsed_value) FILTER (
                    WHERE parsed_value BETWEEN {VALID_LOW_C} AND {VALID_HIGH_C}
                ) AS value_mean,
                max(CAST(
                    parsed_value BETWEEN {VALID_LOW_C} AND {VALID_HIGH_C}
                    AND (parsed_value < {LOW_C} OR parsed_value > {HIGH_C})
                    AS INTEGER
                )) AS bad
            FROM temp_raw
            GROUP BY channel, ts
        """)
        con.execute("""
            CREATE TEMP TABLE bounds AS
            SELECT channel,
                date_trunc('day', min(ts)) + INTERVAL 1 DAY AS first_day,
                date_trunc('day', max(ts)) AS last_day
            FROM temp_seconds
            GROUP BY channel
        """)
        con.execute("""
            CREATE TEMP TABLE grid AS
            SELECT channel, generate_series AS as_of
            FROM bounds,
            generate_series(first_day, last_day, INTERVAL 1 DAY)
        """)
        for hours in WINDOW_HOURS:
            con.execute(_window_sql(hours))
        con.execute("""
            CREATE TEMP TABLE future AS
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
        select_columns.extend(
            [
                "future.future_observed_seconds",
                "future.future_numeric_records",
                "future.future_bad_seconds",
            ]
        )
        con.execute(f"""
            COPY (
                SELECT {', '.join(select_columns)}
                FROM grid g
                {joins}
                JOIN future USING (channel, as_of)
                ORDER BY as_of, channel
            ) TO '{escaped_output}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        raw_stats = con.execute("""
            SELECT count(*) AS rows, count(DISTINCT channel) AS channels,
                count(*) FILTER (WHERE parsed_value IS NOT NULL) AS parsed_rows,
                count(*) FILTER (
                    WHERE parsed_value BETWEEN ? AND ?
                ) AS valid_numeric_rows
            FROM temp_raw
        """, [VALID_LOW_C, VALID_HIGH_C]).fetchone()
        feature_stats = con.execute(
            f"SELECT count(*), min(as_of), max(as_of) FROM read_parquet('{escaped_output}')"
        ).fetchone()
        sources = con.execute("""
            SELECT filename, sha256, time_zone
            FROM ingest_sources ORDER BY filename
        """).fetchall()

    manifest = {
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
        "excluded_period": [MIGRATION_START, MIGRATION_END],
        "raw_rows": raw_stats[0],
        "channels": raw_stats[1],
        "parsed_rows": raw_stats[2],
        "valid_numeric_rows": raw_stats[3],
        "feature_rows": feature_stats[0],
        "as_of_min": str(feature_stats[1]),
        "as_of_max": str(feature_stats[2]),
        "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "sources": [
            {"filename": filename, "sha256": sha256, "time_zone": timezone}
            for filename, sha256, timezone in sources
        ],
        "limitations": [
            "The [3,40] C range is inferred from sensor text and is not confirmed by the owner.",
            "An out-of-range measurement is a proxy episode, not a confirmed equipment failure.",
            "The current catalog is applied to history without historical equipment versions.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.database, args.catalog, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
