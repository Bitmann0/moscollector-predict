"""Reuse normalized annual sources for the existing pump signal experiment."""

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd


def prepare(database: Path, catalog: Path, output: Path, years: list[int]) -> dict:
    if not years:
        raise ValueError("At least one year required")
    channels = pd.read_csv(catalog)
    pump_ids = channels.loc[
        channels["тип_датчика"].eq("Состояние насоса"), ["ид_канала_данных"]
    ].rename(columns={"ид_канала_данных": "channel"})
    if pump_ids.channel.duplicated().any():
        raise ValueError("Duplicate pump catalog IDs")
    names = [f"ext-journal-{year}.csv" for year in sorted(set(years))]
    marks = ",".join("?" for _ in names)
    with duckdb.connect(str(database), read_only=True) as con:
        con.execute("SET memory_limit='2GB'")
        con.execute("SET threads=4")
        sources = con.execute(
            f"SELECT sha256,time_zone,report FROM ingest_sources WHERE filename IN ({marks}) ORDER BY filename",
            names,
        ).fetchall()
        if len(sources) != len(names) or any(row[1] != "Europe/Moscow" for row in sources):
            raise ValueError("Required annual sources / timezone not found")
        shas = [row[0] for row in sources]
        where = f"source_sha256 IN ({marks}) AND sensor_value IS NOT NULL"
        con.register("pump_ids", pump_ids)
        coverage = con.execute(
            f"""SELECT local_date AS day,count(*) global_events,
            count(DISTINCT hour(event_time AT TIME ZONE 'Europe/Moscow')) observed_hours
            FROM events WHERE {where} GROUP BY 1 ORDER BY 1""",
            shas,
        ).df()
        con.execute(
            f"""CREATE TEMP TABLE pumps AS SELECT DISTINCT channel_id channel,
            event_time AT TIME ZONE 'Europe/Moscow' ts, alarm, sensor_value
            FROM events JOIN pump_ids ON channel_id=pump_ids.channel
            WHERE {where}""",
            shas,
        )
        daily = con.execute("""WITH seconds AS (
            SELECT channel,ts,count(*) records,sum(CAST(alarm AS INT)) alarms,
                max(CAST(sensor_value='Неисправен' AS INT)) fault,
                max(CAST(sensor_value='Неопределен' AS INT)) undefined,
                max(CAST(sensor_value='Обесточен' AS INT)) power_loss,
                max(CAST(sensor_value='Включен' AS INT)) running,
                CAST(count(DISTINCT sensor_value)>1 AS INT) ambiguous
            FROM pumps GROUP BY channel,ts)
            SELECT channel,CAST(ts AS DATE) AS day,sum(records) events,sum(alarms) alarms,
                sum(fault) faults,sum(undefined) undefined,sum(power_loss) power_loss,
                sum(running) running,sum(ambiguous) ambiguous_seconds
            FROM seconds GROUP BY 1,2 ORDER BY 1,2""").df()
        audit = (
            con.execute("""WITH seconds AS (
            SELECT channel,ts,max(CAST(sensor_value='Неисправен' AS INT)) fault,
                count(DISTINCT sensor_value) states
            FROM pumps GROUP BY channel,ts)
            SELECT count(*) observed_seconds,count(*) FILTER (WHERE fault=1) fault_seconds,
                count(*) FILTER (WHERE fault=1 AND states>1) mixed_fault_seconds
            FROM seconds""")
            .df()
            .iloc[0]
            .astype(int)
            .to_dict()
        )
        companions = (
            con.execute("""WITH fault_seconds AS (
            SELECT DISTINCT channel,ts FROM pumps WHERE sensor_value='Неисправен')
            SELECT sensor_value,count(*) AS seconds FROM (
                SELECT DISTINCT p.channel,p.ts,p.sensor_value
                FROM pumps p JOIN fault_seconds f USING(channel,ts)
                WHERE p.sensor_value<>'Неисправен')
            GROUP BY sensor_value ORDER BY seconds DESC,sensor_value""")
            .df()
            .to_dict("records")
        )
        audit["companion_states"] = companions
        audit["interpretation"] = (
            "Concurrent recorded states do not establish physical failure or temporal order. "
            "Companion counts may overlap; no state is automatically declared invalid."
        )
    output.mkdir(parents=True, exist_ok=True)
    daily.to_csv(output / "pump_daily.csv", index=False, lineterminator="\n")
    coverage.to_csv(output / "coverage.csv", index=False, lineterminator="\n")
    manifest = {
        "sources": [json.loads(row[2]) for row in sources],
        "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "deduplication": "channel,timestamp,alarm,value within selected sources",
        "timezone_assumption": "Europe/Moscow",
        "pump_daily_rows": len(daily),
        "coverage_days": len(coverage),
        "warehouse_adapter_version": "v1",
        "label_audit": audit,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--years", type=int, nargs="+", default=[2024, 2025, 2026])
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.database, args.catalog, args.output, args.years), ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
