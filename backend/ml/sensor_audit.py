"""Audit sensor coverage, value semantics and candidate prediction targets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd

CATEGORICAL_SECOND_AUDIT_TYPES = [
    "9-секционный люк",
    "Датчик дыма",
    "Датчик затопления",
    "ИБП",
    "КД АВ",
    "КД Дверь",
    "КД Люк",
    "Переключатель",
    "Ручной извещатель",
    "Состояние УИР-Р",
    "Состояние вентилятора",
    "Состояние насоса",
    "Состояние охраны",
    "Состояние фазы",
    "Стекло",
    "Тепловой датчик",
]


def _records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def audit(database: Path, catalog_path: Path, output: Path) -> dict:
    catalog = pd.read_csv(catalog_path).rename(
        columns={
            "ид_канала_данных": "channel_id",
            "тип_инж_системы": "system_type",
            "тип_датчика": "sensor_type",
        }
    )[["channel_id", "system_type", "sensor_type"]]
    if catalog.channel_id.duplicated().any():
        raise ValueError("Catalog channel IDs must be unique")

    with duckdb.connect(str(database), read_only=True) as con:
        con.execute("SET memory_limit='3GB'")
        con.execute("SET threads=4")
        con.register("catalog", catalog)
        overview = con.execute("""
            SELECT coalesce(c.sensor_type,'__UNMAPPED__') sensor_type,
                count(DISTINCT d.channel_id) observed_channels,
                count(*) observed_channel_days,
                count(DISTINCT d.local_date) observed_dates,
                sum(d.event_count) events,sum(d.alarm_count) alarms,
                min(d.local_date) first_day,max(d.local_date) last_day
            FROM daily_channels d LEFT JOIN catalog c USING(channel_id)
            GROUP BY 1 ORDER BY events DESC
        """).df()
        yearly = con.execute("""
            SELECT coalesce(c.sensor_type,'__UNMAPPED__') sensor_type,
                year(d.local_date) AS calendar_year,
                count(DISTINCT d.channel_id) observed_channels,
                count(*) observed_channel_days,count(DISTINCT d.local_date) observed_dates,
                sum(d.event_count) events,sum(d.alarm_count) alarms
            FROM daily_channels d LEFT JOIN catalog c USING(channel_id)
            GROUP BY 1,2 ORDER BY 1,2
        """).df()
        values = con.execute("""
            WITH grouped AS (
                SELECT coalesce(c.sensor_type,'__UNMAPPED__') sensor_type,
                    e.sensor_value,count(*) events,count(DISTINCT e.channel_id) channels,
                    sum(CAST(e.alarm AS BIGINT)) alarms,min(e.local_date) first_day,
                    max(e.local_date) last_day
                FROM events e LEFT JOIN catalog c USING(channel_id)
                WHERE e.sensor_value IS NOT NULL
                GROUP BY 1,2
            ), ranked AS (
                SELECT *,row_number() OVER (PARTITION BY sensor_type ORDER BY events DESC,
                    sensor_value) value_rank
                FROM grouped
            )
            SELECT * FROM ranked WHERE value_rank<=30 ORDER BY sensor_type,value_rank
        """).df()
        cardinality = con.execute("""
            SELECT coalesce(c.sensor_type,'__UNMAPPED__') sensor_type,
                count(DISTINCT e.sensor_value) distinct_values,
                count(*) FILTER (WHERE try_cast(replace(e.sensor_value,',','.') AS DOUBLE)
                    IS NOT NULL) numeric_events,
                count(*) non_null_events
            FROM events e LEFT JOIN catalog c USING(channel_id)
            WHERE e.sensor_value IS NOT NULL GROUP BY 1 ORDER BY non_null_events DESC
        """).df()
        continuity = con.execute("""
            WITH spans AS (
                SELECT coalesce(c.sensor_type,'__UNMAPPED__') sensor_type,d.channel_id,
                    min(d.local_date) first_day,max(d.local_date) last_day,
                    count(DISTINCT d.local_date) observed_days,sum(d.event_count) events
                FROM daily_channels d LEFT JOIN catalog c USING(channel_id)
                GROUP BY 1,2
            )
            SELECT sensor_type,count(*) channels,
                median(observed_days) median_observed_days,
                quantile_cont(observed_days,0.1) p10_observed_days,
                quantile_cont(observed_days,0.9) p90_observed_days,
                median(date_diff('day',first_day,last_day)+1) median_span_days,
                count(*) FILTER (WHERE first_day<=DATE '2024-01-07'
                    AND last_day>=DATE '2026-06-23') channels_spanning_model_period
            FROM spans GROUP BY 1 ORDER BY channels DESC
        """).df()
        con.register(
            "categorical_types", pd.DataFrame({"sensor_type": CATEGORICAL_SECOND_AUDIT_TYPES})
        )
        second_states = con.execute("""
            WITH seconds AS (
                SELECT c.sensor_type,e.channel_id,e.event_time,
                    count(DISTINCT e.sensor_value) states,
                    max(CAST(e.alarm AS INTEGER)) alarm,
                    max(CAST(e.sensor_value='Неисправен' AS INTEGER)) fault
                FROM events e JOIN catalog c USING(channel_id)
                JOIN categorical_types t USING(sensor_type)
                WHERE e.local_date BETWEEN DATE '2024-01-01' AND DATE '2026-06-30'
                    AND e.sensor_value IS NOT NULL
                GROUP BY 1,2,3
            )
            SELECT sensor_type,count(*) observed_seconds,
                count(*) FILTER (WHERE states>1) mixed_state_seconds,
                count(*) FILTER (WHERE alarm=1) alarm_seconds,
                count(*) FILTER (WHERE alarm=1 AND states>1) mixed_alarm_seconds,
                count(*) FILTER (WHERE fault=1) fault_seconds,
                count(*) FILTER (WHERE fault=1 AND states>1) mixed_fault_seconds,
                max(states) maximum_states_in_one_second
            FROM seconds GROUP BY 1 ORDER BY observed_seconds DESC
        """).df()

    merged = overview.merge(cardinality, on="sensor_type", how="left").merge(
        continuity, on="sensor_type", how="left", suffixes=("", "_continuity")
    )
    merged["alarm_rate"] = merged.alarms / merged.events
    merged["numeric_event_fraction"] = merged.numeric_events / merged.non_null_events
    report = {
        "scope": {
            "database": database.name,
            "catalog": catalog_path.name,
            "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
            "catalog_channels": len(catalog),
            "catalog_sensor_types": int(catalog.sensor_type.nunique()),
            "note": "Current catalog is applied to all history; historical catalog versions unavailable.",
        },
        "sensor_types": _records(merged),
        "yearly": _records(yearly),
        "top_values": _records(values),
        "same_second_state_audit_2024_2026": _records(second_states),
        "definitions": {
            "observed_channel_days": "Rows in normalized daily_channels; missing rows are not healthy days.",
            "channels_spanning_model_period": "First/last observation cover 2024-01-07 through 2026-06-23; gaps may remain.",
            "alarms": "Source alarm flag count; its business meaning is not independently confirmed.",
            "same_second_state_audit_2024_2026": (
                "Categorical types except high-frequency motion; concurrent values have no reliable order."
            ),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.database, args.catalog, args.output)
    print(json.dumps(result["scope"], ensure_ascii=False))


if __name__ == "__main__":
    main()
