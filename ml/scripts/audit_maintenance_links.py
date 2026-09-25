"""Retrospective evidence for candidate gas-maintenance links, not an ML backtest."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path

import polars as pl

from mkl import serve
from mkl.maintenance import load_mapping


def audit(schedule: dict, mapping: dict, channels: pl.DataFrame,
          daily: pl.DataFrame, link_eval: pl.DataFrame | None = None,
          link_threshold: float | None = None) -> dict:
    gas = channels.filter(pl.col("stype") == "Газовый датчик")
    report = {"audit_type": "retrospective_mapping_evidence_not_asof_backtest",
              "schedule_available_from": schedule["available_from"],
              "mapping_available_from": mapping["available_from"],
              "candidate_links": [], "gas_to_months": []}
    issued = None
    if link_eval is not None:
        if link_threshold is None:
            raise ValueError("A_link policy threshold required with evaluation rows")
        issued = serve.alerts_over_time(link_eval, 20, "ch", cooldown_days=7,
                                        threshold=link_threshold).filter(pl.col("alert"))
    for link in mapping["links"]:
        parent = str(link["obj_parent"])
        gas_channels = set(gas.filter(pl.col("obj_parent") == parent)["ch"].to_list())
        key = link["source_object_key"]
        if key.startswith("ppr:"):
            row = next(r for r in schedule["ppr_objects"] if r["source_object_key"] == key)
            if not row["planned_dismantle"]:
                continue
            start = dt.date.fromisoformat(row["planned_dismantle"])
            relevant = daily.filter(pl.col("ch").is_in(gas_channels)
                                    & pl.col("day").is_between(start - dt.timedelta(days=7),
                                                                start + dt.timedelta(days=7)))
            counts = {r["day"]: r["n"] for r in relevant.filter(pl.col("n_events") > 0)
                      .group_by("day").agg(pl.col("ch").n_unique().alias("n")).to_dicts()}
            before = [counts.get(start - dt.timedelta(days=i), 0) for i in range(7, 0, -1)]
            after = [counts.get(start + dt.timedelta(days=i), 0) for i in range(1, 8)]
            item = {"source_object_key": key, "obj_parent": parent,
                    "mapping_status": link["mapping_status"],
                    "ppr_sensor_count": row["sensor_count"],
                    "catalog_gas_channel_count": len(gas_channels),
                    "planned_dismantle": start.isoformat(),
                    "active_gas_channels_before_7d": before,
                    "active_gas_channels_after_7d": after,
                    "median_active_before": statistics.median(before),
                    "median_active_after": statistics.median(after)}
            if issued is not None and start in link_eval["day"].unique().to_list():
                on_day = issued.filter(pl.col("day") == start)
                matched = on_day.filter(pl.col("ch").is_in(gas_channels))
                item["a_link_policy_on_planned_start"] = {
                    "all_alerts": on_day.height,
                    "mapped_gas_alerts": matched.height,
                    "mapped_gas_proxy_positive": matched.filter(pl.col("y") == 1).height,
                }
            report["candidate_links"].append(item)
        elif key.startswith("to:"):
            equip_rows = {r["source_row"] for r in schedule["to_equipment"]
                          if r["source_object_key"] == key and r["equipment_type"] == "ГАСБ"}
            months = sorted({(r["month"], r["work_type"]) for r in schedule["to_work_months"]
                             if r["source_object_key"] == key
                             and r["equipment_source_row"] in equip_rows})
            report["gas_to_months"].append({
                "source_object_key": key, "obj_parent": parent,
                "source_name": next(r["source_name"] for r in schedule["to_equipment"]
                                    if r["source_object_key"] == key),
                "mapping_status": link["mapping_status"],
                "catalog_gas_channel_count": len(gas_channels),
                "gasb_count": next(r["quantity"] for r in schedule["to_equipment"]
                                   if r["source_object_key"] == key
                                   and r["equipment_type"] == "ГАСБ"),
                "months": [{"month": month, "work_type": work} for month, work in months],
            })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", type=Path, default=Path("data/interim/maintenance_2026.json"))
    parser.add_argument("--mapping", type=Path,
                        default=Path("resources/maintenance_mapping_candidates.json"))
    parser.add_argument("--channels", type=Path, default=Path("data/interim/channels.parquet"))
    parser.add_argument("--daily", type=Path, default=Path("data/interim/daily_channel.parquet"))
    parser.add_argument("--link-eval", type=Path,
                        default=Path("data/tmp/laya_link_full_eval.parquet"))
    parser.add_argument("--link-metadata", type=Path,
                        default=Path("data/tmp/laya_link_metadata.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("reports/maintenance_link_audit.json"))
    args = parser.parse_args()
    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    mapping = load_mapping(args.mapping, schedule)
    meta = json.loads(args.link_metadata.read_text(encoding="utf-8"))
    result = audit(schedule, mapping, pl.read_parquet(args.channels),
                   pl.read_parquet(args.daily), pl.read_parquet(args.link_eval),
                   meta["threshold"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
