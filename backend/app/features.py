"""Build past-only daily features from the normalized annual warehouse."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from .ingest import fingerprint

FEATURE_SQL = """
SELECT *,
    alarm_count / event_count::DOUBLE alarm_fraction,
    lag(local_date) OVER channel_history previous_observed_date,
    date_diff('day', lag(local_date) OVER channel_history, local_date) days_since_previous,
    count(*) OVER prior_30d observed_days_previous_30d,
    sum(event_count) OVER prior_30d events_previous_30d,
    median(event_count) OVER prior_30d median_events_previous_30d,
    sum(alarm_count) OVER prior_30d alarms_previous_30d,
    count(*) OVER prior_7d observed_days_previous_7d,
    sum(event_count) OVER prior_7d events_previous_7d
FROM daily_channels
WINDOW channel_history AS (PARTITION BY channel_id ORDER BY local_date),
    prior_30d AS (PARTITION BY channel_id ORDER BY local_date
        RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING),
    prior_7d AS (PARTITION BY channel_id ORDER BY local_date
        RANGE BETWEEN INTERVAL 7 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING)
"""


def build_features(database: Path, output: Path, catalog: Path | None = None) -> dict:
    """Features are available after the observed day ends, never at its beginning."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database), read_only=True) as con:
        con.execute("SET memory_limit='3GB'")
        con.execute("SET threads=4")
        zones = con.execute("SELECT DISTINCT time_zone FROM ingest_sources").fetchall()
        if len(zones) != 1:
            raise ValueError("Feature export requires one source timezone")
        overlap = con.execute("""SELECT channel_id,local_date FROM daily_channels
            GROUP BY 1,2 HAVING count(*) > 1 LIMIT 1""").fetchone()
        if overlap:
            raise ValueError("Overlapping channel/day sources require reconciliation before export")
        con.execute("CREATE TEMP TABLE catalog(channel_id BIGINT, sensor_type VARCHAR)")
        if catalog:
            con.execute(
                "INSERT INTO catalog SELECT DISTINCT "
                'try_cast("ид_канала_данных" AS BIGINT), "тип_датчика" '
                "FROM read_csv(?,all_varchar=true)",
                [str(catalog)],
            )
            if con.execute(
                "SELECT count(*) FROM catalog WHERE channel_id IS NULL OR channel_id<=0"
            ).fetchone()[0]:
                raise ValueError("Invalid catalog channel ID")
            if con.execute(
                "SELECT channel_id FROM catalog GROUP BY 1 HAVING count(*)>1 LIMIT 1"
            ).fetchone():
                raise ValueError("Conflicting sensor types in catalog")
        con.execute("CREATE TEMP VIEW past_features AS " + FEATURE_SQL)
        con.execute(
            """CREATE TEMP TABLE feature_export AS SELECT p.*, c.sensor_type,
            timezone(?, (local_date + INTERVAL 1 DAY)::TIMESTAMP) feature_available_at,
            event_count / nullif(median_events_previous_30d,0) activity_ratio_to_past,
            observed_days_previous_30d >= 7 baseline_available
            FROM past_features p LEFT JOIN catalog c USING (channel_id)""",
            [zones[0][0]],
        )
        # COPY parameters bind paths rather than interpolate SQL.
        con.execute("COPY feature_export TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(output)])
        rows = con.execute("""SELECT count(*), count(DISTINCT channel_id),
            min(local_date),max(local_date),sum(baseline_available::INT)
            FROM feature_export""").fetchone()
        sources = con.execute("SELECT sha256 FROM ingest_sources ORDER BY sha256").fetchall()
        report = {
            "rows": rows[0],
            "channels": rows[1],
            "date_from": str(rows[2]),
            "date_to": str(rows[3]),
            "rows_with_7_prior_observed_days": rows[4],
            "source_sha256": [r[0] for r in sources],
            "time_zone": zones[0][0],
            "target_available": False,
            "missing_days_policy": "unobserved; not filled as healthy or zero activity",
            "feature_version": "daily-past-only-v1",
            "catalog_sha256": fingerprint(catalog) if catalog else None,
            "unknown_type_rows": con.execute(
                "SELECT count(*) FROM feature_export WHERE sensor_type IS NULL"
            ).fetchone()[0],
        }
        output.with_suffix(".json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_features(args.database, args.output, args.catalog), ensure_ascii=False))


if __name__ == "__main__":
    main()
