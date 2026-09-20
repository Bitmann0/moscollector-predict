"""Build a strict temporary-telemetry-outage target from observed daily features."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb

MIGRATION_START = "2021-04-01"
MIGRATION_FEATURE_RECOVERY_END = "2021-08-01"


def prepare(
    daily_features: Path, output: Path, recovery_days: int = 7, min_active_days: int = 25
) -> dict:
    if recovery_days < 2:
        raise ValueError("recovery_days must be at least 2")
    if not 1 <= min_active_days <= 30:
        raise ValueError("min_active_days must be between 1 and 30")
    output.mkdir(parents=True, exist_ok=True)
    sample_path = output / "availability_samples.parquet"
    escaped_input = str(daily_features.resolve()).replace("'", "''")
    escaped_output = str(sample_path.resolve()).replace("'", "''")
    with duckdb.connect() as con:
        con.execute("SET memory_limit='4GB'")
        con.execute("SET threads=4")
        con.execute(f"""
            CREATE TEMP TABLE source AS
            SELECT * FROM read_parquet('{escaped_input}')
        """)
        duplicate_days = con.execute("""
            SELECT count(*) FROM (
                SELECT channel_id, local_date FROM source
                GROUP BY 1,2 HAVING count(*) > 1
            )
        """).fetchone()[0]
        if duplicate_days:
            raise ValueError(f"Duplicate channel-days: {duplicate_days}")
        con.execute("""
            CREATE TEMP TABLE calendar AS
            WITH bounds AS (SELECT min(local_date) lo, max(local_date) hi FROM source),
            counts AS (
                SELECT local_date AS day, count(DISTINCT channel_id) AS observed_channels
                FROM source GROUP BY 1
            )
            SELECT day,
                coalesce(observed_channels, 0) AS observed_channels,
                median(coalesce(observed_channels, 0)) OVER (
                    ORDER BY day ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
                ) AS previous_30d_median_channels
            FROM bounds,
            generate_series(lo, hi, INTERVAL 1 DAY) dates(day)
            LEFT JOIN counts USING (day)
        """)
        con.execute("""
            CREATE TEMP TABLE ordered AS
            SELECT *,
                lead(local_date) OVER (
                    PARTITION BY channel_id ORDER BY local_date
                ) AS next_observed_date
            FROM source
        """)
        con.execute(f"""
            COPY (
                SELECT o.*,
                    o.local_date + INTERVAL 1 DAY AS target_day,
                    o.local_date + INTERVAL 2 DAY AS label_end,
                    date_diff('day', o.local_date, o.next_observed_date) AS next_gap_days,
                    c.observed_channels AS target_global_observed_channels,
                    c.previous_30d_median_channels AS target_global_previous_median,
                    CAST(o.next_observed_date > o.local_date + INTERVAL 1 DAY AS INTEGER)
                        AS target
                FROM ordered o
                JOIN calendar c ON c.day = o.local_date + INTERVAL 1 DAY
                WHERE o.baseline_available
                  AND o.observed_days_previous_30d >= {min_active_days}
                  AND c.observed_channels > 0
                  AND c.observed_channels >= 0.5 * c.previous_30d_median_channels
                  AND (
                    o.next_observed_date = o.local_date + INTERVAL 1 DAY
                    OR o.next_observed_date BETWEEN o.local_date + INTERVAL 2 DAY
                                                AND o.local_date + INTERVAL {recovery_days} DAY
                  )
                  AND NOT (
                    o.local_date >= DATE '{MIGRATION_START}'
                    AND o.local_date < DATE '{MIGRATION_FEATURE_RECOVERY_END}'
                  )
                ORDER BY o.local_date, o.channel_id
            ) TO '{escaped_output}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        stats = con.execute(f"""
            SELECT count(*) row_count, sum(target) positives,
                count(DISTINCT channel_id) channels,
                count(DISTINCT channel_id) FILTER (WHERE target=1) positive_channels,
                min(local_date), max(local_date)
            FROM read_parquet('{escaped_output}')
        """).fetchone()
        coverage = con.execute("""
            SELECT count(*) day_count,
                count(*) FILTER (
                    WHERE observed_channels > 0
                      AND observed_channels >= 0.5 * previous_30d_median_channels
                ) usable_day_count,
                min(day), max(day)
            FROM calendar
        """).fetchone()
    manifest = {
        "target": (
            "No telemetry on the next globally covered calendar day after at least "
            f"{min_active_days} observed "
            f"days in the previous 30, with telemetry recovery within {recovery_days} days"
        ),
        "target_name": "temporary_telemetry_outage_onset",
        "positive_semantics": "availability incident, not a confirmed physical sensor failure",
        "recovery_days": recovery_days,
        "min_active_days_previous_30d": min_active_days,
        "rows": stats[0],
        "positives": stats[1],
        "positive_rate": stats[1] / stats[0] if stats[0] else None,
        "channels": stats[2],
        "positive_channels": stats[3],
        "date_min": str(stats[4]),
        "date_max": str(stats[5]),
        "calendar_days": coverage[0],
        "usable_global_days": coverage[1],
        "calendar_min": str(coverage[2]),
        "calendar_max": str(coverage[3]),
        "global_coverage_rule": "observed channels >= 50% of trailing 30-day median",
        "excluded_feature_dates": [MIGRATION_START, MIGRATION_FEATURE_RECOVERY_END],
        "input_sha256": hashlib.sha256(daily_features.read_bytes()).hexdigest(),
        "limitations": [
            "Recovery confirms a temporary telemetry outage, not its physical cause.",
            "Daily features use the current channel catalog on historical records.",
            "Outages longer than the recovery window are censored as unknown.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-features", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recovery-days", type=int, default=7)
    parser.add_argument("--min-active-days", type=int, default=25)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.daily_features,
                args.output,
                args.recovery_days,
                args.min_active_days,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
