"""Reconcile imported counts with the independently recorded full-data audit."""

import argparse
import json
from pathlib import Path

import duckdb


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--audit", default="analysis/annual_profiles.json", type=Path)
    parser.add_argument("--output", default="analysis/warehouse_verification.json", type=Path)
    args = parser.parse_args()
    audited = {
        f"ext-journal-{row['year']}.csv": row["summary"]["row_count"]
        for row in json.loads(args.audit.read_text(encoding="utf-8"))
    }
    with duckdb.connect(str(args.database), read_only=True) as con:
        reports = [
            json.loads(row[0])
            for row in con.execute("SELECT report FROM ingest_sources ORDER BY filename").fetchall()
        ]
        if {r["filename"] for r in reports} != set(audited):
            raise ValueError(
                "Imported source set differs from independently audited annual sources"
            )
        for row in reports:
            assert row["raw_rows"] == audited[row["filename"]], row["filename"]
            assert row["raw_rows"] == (
                row["accepted_rows"]
                + row["exact_duplicate_rows"]
                + row["embedded_headers"]
                + row["rejected_source_rows"]
            )
        events = con.execute("SELECT count(*),sum(occurrences) FROM events").fetchone()
        daily = con.execute(
            "SELECT sum(event_count),count(*),count(DISTINCT channel_id) FROM daily_channels"
        ).fetchone()
        assert events[0] == daily[0] == sum(r["accepted_rows"] for r in reports)
        assert events[1] == sum(r["accepted_source_rows"] for r in reports)
        report = {
            "audit_row_counts_match": True,
            "source_count": len(reports),
            "raw_rows": sum(audited.values()),
            "accepted_rows": events[0],
            "exact_duplicate_rows": sum(r["exact_duplicate_rows"] for r in reports),
            "embedded_headers": sum(r["embedded_headers"] for r in reports),
            "rejected_source_rows": sum(r["rejected_source_rows"] for r in reports),
            "daily_channel_source_rows": daily[1],
            "observed_channels": daily[2],
            "database_bytes": args.database.stat().st_size,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
