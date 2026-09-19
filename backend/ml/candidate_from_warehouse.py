"""Prepare a selected sensor type and candidate daily target from normalized history."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd

TARGETS = {
    "gas_alarm": {
        "sensor_type": "Газовый датчик",
        "kind": "alarm",
        "description": "At least one source alarm=true event on a gas channel",
    },
    "phase_fault": {
        "sensor_type": "Состояние фазы",
        "kind": "value",
        "values": ["Неисправен"],
        "description": "At least one recorded Неисправен state on a phase channel",
    },
    "smoke_fault": {
        "sensor_type": "Датчик дыма",
        "kind": "value",
        "values": ["Неисправен"],
        "description": "At least one recorded Неисправен state on a smoke channel",
    },
    "temperature_outside_3_40": {
        "sensor_type": "Датчик температуры",
        "kind": "numeric_outside",
        "low": 3.0,
        "high": 40.0,
        "description": "At least one numeric value below 3 or above 40 on a temperature channel",
    },
}


def prepare(
    database: Path, catalog_path: Path, output: Path, candidate: str, years: list[int]
) -> dict:
    if candidate not in TARGETS:
        raise ValueError(f"Unknown candidate: {candidate}")
    if not years:
        raise ValueError("At least one year required")
    definition = TARGETS[candidate]
    catalog = pd.read_csv(catalog_path)
    selected_ids = catalog.loc[
        catalog["тип_датчика"].eq(definition["sensor_type"]), ["ид_канала_данных"]
    ].rename(columns={"ид_канала_данных": "channel"})
    names = [f"ext-journal-{year}.csv" for year in sorted(set(years))]
    marks = ",".join("?" for _ in names)
    with duckdb.connect(str(database), read_only=True) as con:
        con.execute("SET memory_limit='3GB'")
        con.execute("SET threads=4")
        sources = con.execute(
            f"SELECT sha256,time_zone,report FROM ingest_sources "
            f"WHERE filename IN ({marks}) ORDER BY filename",
            names,
        ).fetchall()
        if len(sources) != len(names) or any(row[1] != "Europe/Moscow" for row in sources):
            raise ValueError("Required annual sources / timezone not found")
        shas = [row[0] for row in sources]
        where = f"source_sha256 IN ({marks}) AND sensor_value IS NOT NULL"
        con.register("selected_ids", selected_ids)
        coverage = con.execute(
            f"""SELECT local_date AS day,count(*) global_events,
            count(DISTINCT hour(event_time AT TIME ZONE 'Europe/Moscow')) observed_hours
            FROM events WHERE {where} GROUP BY 1 ORDER BY 1""",
            shas,
        ).df()
        con.execute(
            f"""CREATE TEMP TABLE selected AS SELECT DISTINCT channel_id channel,
            event_time AT TIME ZONE 'Europe/Moscow' ts,alarm,sensor_value
            FROM events JOIN selected_ids ON channel_id=selected_ids.channel WHERE {where}""",
            shas,
        )
        if definition["kind"] == "value":
            con.register("target_values", pd.DataFrame({"value": definition["values"]}))
            target_sql = "sensor_value IN (SELECT value FROM target_values)"
        elif definition["kind"] == "alarm":
            target_sql = "alarm"
        else:
            target_sql = (
                f"try_cast(replace(sensor_value,',','.') AS DOUBLE)<{definition['low']} OR "
                f"try_cast(replace(sensor_value,',','.') AS DOUBLE)>{definition['high']}"
            )
        daily = con.execute(f"""WITH seconds AS (
            SELECT channel,ts,count(*) records,sum(CAST(alarm AS INT)) alarms,
                max(CAST(({target_sql}) AS INT)) target_flag,
                max(CAST(sensor_value='Неопределен' AS INT)) undefined,
                max(CAST(sensor_value='Обесточен' AS INT)) power_loss,
                max(CAST(sensor_value='Включен' AS INT)) running,
                CAST(count(DISTINCT sensor_value)>1 AS INT) ambiguous
            FROM selected GROUP BY channel,ts)
            SELECT channel,CAST(ts AS DATE) AS day,sum(records) AS events,
                sum(alarms) AS alarms,sum(target_flag) AS faults,
                sum(undefined) AS undefined,sum(power_loss) AS power_loss,
                sum(running) AS running,sum(ambiguous) AS ambiguous_seconds
            FROM seconds GROUP BY 1,2 ORDER BY 1,2""").df()
    output.mkdir(parents=True, exist_ok=True)
    daily.to_csv(output / "pump_daily.csv", index=False, lineterminator="\n")
    coverage.to_csv(output / "coverage.csv", index=False, lineterminator="\n")
    manifest = {
        "candidate": candidate,
        "sensor_type": definition["sensor_type"],
        "target_description": definition["description"],
        "sources": [json.loads(row[2]) for row in sources],
        "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "daily_rows": len(daily),
        "channels": int(daily.channel.nunique()),
        "coverage_days": len(coverage),
        "limitations": [
            "The target is a recorded message or inferred numeric condition, not a confirmed incident.",
            "The current catalog is applied to historical data without versioning.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", choices=sorted(TARGETS), required=True)
    parser.add_argument("--years", type=int, nargs="+", default=[2024, 2025, 2026])
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.database, args.catalog, args.output, args.candidate, args.years),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
