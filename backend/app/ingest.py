"""Transactional, resumable ingestion of extracted annual CSV files into DuckDB."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

COLUMNS = ["ид_события", "ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"]


def fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ingest(path: Path, database: Path, time_zone: str = "Europe/Moscow") -> dict:
    """Commit one source atomically. Identical bytes are imported only once per timezone."""
    ZoneInfo(time_zone)
    before = path.stat()
    digest = fingerprint(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database)) as con:
        con.execute("SET memory_limit='3GB'")
        con.execute("SET threads=4")
        con.execute("SET TimeZone='UTC'")
        con.execute("""CREATE TABLE IF NOT EXISTS ingest_sources (
            sha256 VARCHAR PRIMARY KEY, filename VARCHAR, bytes BIGINT,
            time_zone VARCHAR, report JSON, imported_at TIMESTAMPTZ DEFAULT current_timestamp
        )""")
        previous = con.execute(
            "SELECT time_zone, report FROM ingest_sources WHERE sha256=?", [digest]
        ).fetchone()
        if previous:
            if previous[0] != time_zone:
                raise ValueError("Source already imported with a different timezone")
            return {**json.loads(previous[1]), "skipped": True}
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "CREATE TEMP TABLE source_raw AS SELECT row_number() OVER () AS source_row, * "
                "FROM read_csv(?, all_varchar=true, header=true, strict_mode=true, "
                "ignore_errors=false, nullstr='')",
                [str(path.resolve())],
            )
            actual = [row[0] for row in con.execute("DESCRIBE source_raw").fetchall()][1:]
            if actual != COLUMNS:
                raise ValueError(f"Unexpected CSV columns: {actual}")
            header = " AND ".join(f'"{c}" = ?' for c in COLUMNS)
            headers = con.execute(
                f"SELECT count(*) FROM source_raw WHERE {header}", COLUMNS
            ).fetchone()[0]
            raw_count = con.execute("SELECT count(*) FROM source_raw").fetchone()[0]
            con.execute(f"DELETE FROM source_raw WHERE {header}", COLUMNS)
            quoted = ", ".join(f'"{c}"' for c in COLUMNS)
            # Group on every raw field. ID reuse and simultaneous different states survive.
            con.execute(f"""CREATE TEMP TABLE distinct_raw AS
                SELECT min(source_row) source_row, count(*) occurrences, {quoted}
                FROM source_raw GROUP BY {quoted}""")
            con.execute("""CREATE TEMP TABLE parsed AS SELECT *,
                CASE WHEN regexp_full_match("ид_события", '[0-9]+')
                    THEN try_cast("ид_события" AS BIGINT) END event_id,
                CASE WHEN regexp_full_match("ид_канала_данных", '[0-9]+')
                    THEN try_cast("ид_канала_данных" AS BIGINT) END channel_id,
                try_strptime("дата" || ' ' || "время", '%Y-%m-%d %H:%M:%S') local_ts,
                CASE lower(trim("тревожное"))
                    WHEN 't' THEN true WHEN 'true' THEN true WHEN '1' THEN true
                    WHEN 'f' THEN false WHEN 'false' THEN false WHEN '0' THEN false END alarm
                FROM distinct_raw""")
            valid = "event_id > 0 AND channel_id > 0 AND local_ts IS NOT NULL AND alarm IS NOT NULL"
            table = "events_" + digest
            reject_table = "rejects_" + digest
            con.execute(
                f"""CREATE TABLE {table} AS SELECT
                source_row, occurrences, event_id, channel_id,
                timezone(?, local_ts) event_time, local_ts::DATE local_date,
                alarm, "значение_датчика" sensor_value,
                "дата" date_raw, "время" time_raw, "тревожное" alarm_raw
                FROM parsed WHERE coalesce({valid}, false)""",
                [time_zone],
            )
            con.execute(f"""CREATE TABLE {reject_table} AS SELECT *,
                concat_ws(';', CASE WHEN event_id IS NULL OR event_id <= 0 THEN 'event_id' END,
                    CASE WHEN channel_id IS NULL OR channel_id <= 0 THEN 'channel_id' END,
                    CASE WHEN local_ts IS NULL THEN 'timestamp' END,
                    CASE WHEN alarm IS NULL THEN 'alarm' END) rejection_reason
                FROM parsed WHERE NOT coalesce({valid}, false)""")
            row = con.execute(f"""SELECT count(*), coalesce(sum(occurrences),0),
                min(event_time), max(event_time), count(DISTINCT channel_id)
                FROM {table}""").fetchone()
            rejected = con.execute(
                f"SELECT coalesce(sum(occurrences),0) FROM {reject_table}"
            ).fetchone()[0]
            conflicts = con.execute(f"""SELECT count(*) FROM
                (SELECT event_id FROM {table} GROUP BY event_id HAVING count(*) > 1)""").fetchone()[
                0
            ]
            report = {
                "filename": path.name,
                "sha256": digest,
                "time_zone": time_zone,
                "raw_rows": raw_count,
                "embedded_headers": headers,
                "accepted_rows": row[0],
                "accepted_source_rows": row[1],
                "exact_duplicate_rows": row[1] - row[0],
                "rejected_source_rows": rejected,
                "ambiguous_event_ids": conflicts,
                "channels": row[4],
                "data_from": str(row[2]),
                "data_to": str(row[3]),
                "skipped": False,
            }
            assert raw_count == headers + row[1] + rejected
            if (
                path.stat().st_size != before.st_size
                or path.stat().st_mtime_ns != before.st_mtime_ns
            ):
                raise ValueError("Source changed during import")
            con.execute(
                "INSERT INTO ingest_sources(sha256,filename,bytes,time_zone,report) "
                "VALUES (?,?,?,?,?)",
                [
                    digest,
                    path.name,
                    before.st_size,
                    time_zone,
                    json.dumps(report, ensure_ascii=False),
                ],
            )
            sources = [
                r[0]
                for r in con.execute("SELECT sha256 FROM ingest_sources ORDER BY sha256").fetchall()
            ]
            con.execute(
                "CREATE OR REPLACE VIEW events AS "
                + " UNION ALL ".join(
                    f"SELECT '{sha}' source_sha256, * FROM events_{sha}" for sha in sources
                )
            )
            con.execute(f"""CREATE TABLE daily_{digest} AS
                SELECT channel_id, local_date, count(*) event_count, sum(alarm::INT) alarm_count,
                min(event_time) first_event, max(event_time) last_event
                FROM {table} GROUP BY channel_id, local_date""")
            con.execute(
                "CREATE OR REPLACE VIEW daily_channels AS "
                + " UNION ALL ".join(
                    f"SELECT '{sha}' source_sha256, * FROM daily_{sha}" for sha in sources
                )
            )
            con.execute("COMMIT")
            return report
        except Exception:
            con.execute("ROLLBACK")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--time-zone", default="Europe/Moscow")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    reports = []
    for source in args.sources:
        report = ingest(source, args.database, args.time_zone)
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
            )


if __name__ == "__main__":
    main()
