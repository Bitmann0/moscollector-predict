"""Measure candidate 24-hour targets before spending effort on model tuning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

CANDIDATE_TYPES = {
    "door_open_signal": "КД Дверь",
    "fan_fault_signal": "Состояние вентилятора",
    "gas_alarm_flag": "Газовый датчик",
    "phase_fault_signal": "Состояние фазы",
    "pump_fault_signal": "Состояние насоса",
    "smoke_detected_signal": "Датчик дыма",
    "smoke_fault_signal": "Датчик дыма",
    "temperature_outside_3_40": "Датчик температуры",
    "ups_battery_fault_signal": "ИБП",
    "ups_on_battery_signal": "ИБП",
}


def _records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def audit(database: Path, catalog_path: Path, output: Path) -> dict:
    catalog = pd.read_csv(catalog_path).rename(
        columns={"ид_канала_данных": "channel_id", "тип_датчика": "sensor_type"}
    )[["channel_id", "sensor_type"]]
    with duckdb.connect(str(database), read_only=True) as con:
        con.execute("SET memory_limit='3GB'")
        con.execute("SET threads=4")
        con.register("catalog", catalog)
        con.register(
            "candidate_types",
            pd.DataFrame(
                [(name, sensor_type) for name, sensor_type in CANDIDATE_TYPES.items()],
                columns=["candidate", "sensor_type"],
            ),
        )
        con.execute("""CREATE TEMP TABLE target_days AS
            WITH classified AS (
                SELECT e.channel_id,e.local_date,
                    CASE
                        WHEN c.sensor_type='КД Дверь' AND e.sensor_value='Не замкнут'
                            THEN 'door_open_signal'
                        WHEN c.sensor_type='Состояние вентилятора'
                            AND e.sensor_value='Неисправен' THEN 'fan_fault_signal'
                        WHEN c.sensor_type='Газовый датчик' AND e.alarm THEN 'gas_alarm_flag'
                        WHEN c.sensor_type='Состояние фазы' AND e.sensor_value='Неисправен'
                            THEN 'phase_fault_signal'
                        WHEN c.sensor_type='Состояние насоса' AND e.sensor_value='Неисправен'
                            THEN 'pump_fault_signal'
                        WHEN c.sensor_type='Датчик дыма' AND e.sensor_value='Обнаружен дым'
                            THEN 'smoke_detected_signal'
                        WHEN c.sensor_type='Датчик дыма' AND e.sensor_value='Неисправен'
                            THEN 'smoke_fault_signal'
                        WHEN c.sensor_type='Датчик температуры'
                            AND (try_cast(replace(e.sensor_value,',','.') AS DOUBLE)<3
                                OR try_cast(replace(e.sensor_value,',','.') AS DOUBLE)>40)
                            THEN 'temperature_outside_3_40'
                        WHEN c.sensor_type='ИБП'
                            AND e.sensor_value IN ('Батарея неисправна','Батарея разряжена')
                            THEN 'ups_battery_fault_signal'
                        WHEN c.sensor_type='ИБП' AND e.sensor_value='Питание от батарей'
                            THEN 'ups_on_battery_signal'
                    END candidate
                FROM events e JOIN catalog c USING(channel_id)
            )
            SELECT candidate,channel_id,local_date,count(*) target_events
            FROM classified WHERE candidate IS NOT NULL GROUP BY 1,2,3""")
        con.execute("""CREATE TEMP TABLE observed_days AS
            SELECT t.candidate,d.channel_id,d.local_date
            FROM daily_channels d JOIN catalog c USING(channel_id)
            JOIN candidate_types t USING(sensor_type)
            GROUP BY 1,2,3""")
        summary = con.execute("""
            WITH observed AS (
                SELECT candidate,count(*) observed_channel_days,
                    count(DISTINCT channel_id) observed_channels FROM observed_days GROUP BY 1),
            target AS (
                SELECT candidate,count(*) target_channel_days,sum(target_events) target_events,
                    count(DISTINCT channel_id) target_channels,min(local_date) first_target_day,
                    max(local_date) last_target_day FROM target_days GROUP BY 1),
            transitions AS (
                SELECT t.candidate,
                    count(*) FILTER (WHERE nt.channel_id IS NOT NULL) repeated_next_day,
                    count(*) FILTER (WHERE po.channel_id IS NOT NULL
                        AND pt.channel_id IS NULL) starts_after_observed_non_target_day
                FROM target_days t
                LEFT JOIN target_days nt ON nt.candidate=t.candidate AND nt.channel_id=t.channel_id
                    AND nt.local_date=t.local_date+1
                LEFT JOIN observed_days po ON po.candidate=t.candidate AND po.channel_id=t.channel_id
                    AND po.local_date=t.local_date-1
                LEFT JOIN target_days pt ON pt.candidate=t.candidate AND pt.channel_id=t.channel_id
                    AND pt.local_date=t.local_date-1
                GROUP BY 1)
            SELECT o.candidate,o.observed_channels,o.observed_channel_days,
                coalesce(t.target_channels,0) target_channels,
                coalesce(t.target_channel_days,0) target_channel_days,
                coalesce(t.target_events,0) target_events,t.first_target_day,t.last_target_day,
                coalesce(x.repeated_next_day,0) repeated_next_day,
                coalesce(x.starts_after_observed_non_target_day,0)
                    starts_after_observed_non_target_day,
                coalesce(t.target_channel_days,0)/o.observed_channel_days target_day_rate
            FROM observed o LEFT JOIN target t USING(candidate)
            LEFT JOIN transitions x USING(candidate) ORDER BY target_day_rate DESC
        """).df()
        yearly = con.execute("""
            SELECT o.candidate,year(o.local_date) calendar_year,count(*) observed_channel_days,
                count(t.channel_id) target_channel_days,count(DISTINCT t.channel_id) target_channels
            FROM observed_days o LEFT JOIN target_days t USING(candidate,channel_id,local_date)
            GROUP BY 1,2 ORDER BY 1,2
        """).df()
        concentration = con.execute("""
            WITH counts AS (
                SELECT candidate,channel_id,count(*) target_days FROM target_days GROUP BY 1,2),
            ranked AS (
                SELECT *,row_number() OVER (PARTITION BY candidate ORDER BY target_days DESC)
                    channel_rank,sum(target_days) OVER (PARTITION BY candidate) total_days
                FROM counts)
            SELECT candidate,max(total_days) target_days,
                sum(target_days) FILTER (WHERE channel_rank<=1)/max(total_days) top_1_share,
                sum(target_days) FILTER (WHERE channel_rank<=5)/max(total_days) top_5_share,
                sum(target_days) FILTER (WHERE channel_rank<=10)/max(total_days) top_10_share
            FROM ranked GROUP BY 1 ORDER BY 1
        """).df()
    report = {
        "candidates": _records(summary),
        "yearly": _records(yearly),
        "channel_concentration": _records(concentration),
        "target_definitions": {
            "*_signal": "Presence of the named recorded state during a local calendar day.",
            "gas_alarm_flag": "At least one source alarm=true event on a gas channel.",
            "temperature_outside_3_40": (
                "Numeric recorded value below 3 or above 40; hypothesis inferred from the "
                "recorded text 'В норме от +3 до +40', not a confirmed equipment threshold."
            ),
            "starts_after_observed_non_target_day": (
                "Target day preceded by an observed calendar day for the channel without target."
            ),
        },
        "limitations": [
            "All targets are messages, source flags or inferred thresholds, not confirmed incidents.",
            "Current catalog is applied to history without historical versions.",
            "Daily presence collapses repeated records and does not reconstruct physical episodes.",
        ],
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
    print(json.dumps(result["candidates"], ensure_ascii=False))


if __name__ == "__main__":
    main()
