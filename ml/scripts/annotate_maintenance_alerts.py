"""Emit an as-of-safe maintenance sidecar for existing ML alert JSONL.

The sidecar never edits risk, rank, alert status, or work-order decisions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from mkl.maintenance import load_mapping, maintenance_context


def annotate(alert: dict, schedule: dict, mapping: dict) -> dict:
    address = alert.get("address") or {}
    valid_from = dt.datetime.fromisoformat(alert["valid_from"])
    valid_to = dt.datetime.fromisoformat(alert["valid_to"])
    if valid_to <= valid_from:
        raise ValueError("alert valid_to must be after valid_from")
    # Contract forecast windows are half-open; the context API takes whole days.
    last_day = (valid_to - dt.timedelta(microseconds=1)).date()
    return {
        "alert_id": alert["alert_id"],
        "maintenance_context": maintenance_context(
            schedule, mapping,
            obj_parent=address.get("obj_parent"),
            sensor_type=address.get("sensor_type"),
            asof=dt.date.fromisoformat(alert["asof"]),
            window_start=valid_from.date(), window_end=last_day),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--mapping", type=Path,
                        default=Path("resources/maintenance_mapping_candidates.json"))
    parser.add_argument("--input", type=Path, required=True,
                        help="Existing ML alert JSONL")
    parser.add_argument("--output", type=Path, required=True,
                        help="Alert-id keyed context JSONL")
    args = parser.parse_args()
    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    mapping = load_mapping(args.mapping, schedule)
    with args.input.open(encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
        for line in source:
            if line.strip():
                result = annotate(json.loads(line), schedule, mapping)
                target.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
