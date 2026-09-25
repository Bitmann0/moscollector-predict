"""Safe, source-aware intake of planned maintenance spreadsheets.

The two spreadsheets use local ordinal numbers, not catalog object IDs.  This
module deliberately does not create an automatic catalog join.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


PPR_OBJECT = re.compile(r"^Объект\s+(\d+)$", re.IGNORECASE)
WORK_MARKERS = {"ТО", "ТР", "ТО+ТР"}


def _date(value: object) -> str | None:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return None


def _value(row: tuple, index: int) -> object:
    return row[index] if index < len(row) else None


def parse_ppr(rows: Iterable[tuple[int, tuple]]) -> list[dict]:
    """Normalize PPR rows, preserving their own source ordinal and blanks."""
    result = []
    seen = set()
    for row_number, row in rows:
        match = PPR_OBJECT.fullmatch(str(_value(row, 2) or "").strip())
        if not match:
            continue
        ordinal = int(match.group(1))
        if ordinal in seen:
            raise ValueError(f"duplicate PPR object ordinal {ordinal}")
        seen.add(ordinal)
        quantity = _value(row, 3)
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            raise ValueError(f"PPR row {row_number}: invalid sensor count")
        dates = {
            "planned_dismantle": _date(_value(row, 4)),
            "planned_takeout": _date(_value(row, 6)),
            "planned_commission": _date(_value(row, 7)),
        }
        result.append({
            "source_object_key": f"ppr:{ordinal}",
            "source_row": row_number,
            "source_name": str(_value(row, 2)).strip(),
            "sensor_count": quantity,
            "planned_month_label": _value(row, 1),
            "om_submission_raw": _value(row, 5),
            **dates,
            "quality_flags": (["no_planned_dates"] if not any(dates.values()) else []),
            "internal_obj": None,
            "mapping_status": "unmatched",
        })
    return result


def parse_to(rows: Iterable[tuple[int, tuple, bool]]) -> tuple[list[dict], list[dict]]:
    """Normalize equipment-month markers, retaining hidden source rows."""
    equipment = []
    work = []
    ordinal = None
    source_name = None
    seen = set()
    for row_number, row, hidden in rows:
        block_number = _value(row, 1)
        if isinstance(block_number, int) and not isinstance(block_number, bool):
            if block_number in seen:
                raise ValueError(f"duplicate TO object ordinal {block_number}")
            seen.add(block_number)
            ordinal = block_number
            source_name = str(_value(row, 2) or "").strip() or None
        raw_type = _value(row, 3)
        quantity = _value(row, 4)
        if ordinal is None or not isinstance(raw_type, str):
            continue
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            continue  # repeated page header, not equipment
        equip = {
            "source_object_key": f"to:{ordinal}",
            "source_name": source_name,
            "source_row": row_number,
            "equipment_type": raw_type.strip(),
            "quantity": quantity,
            "unit": str(_value(row, 5) or "").strip(),
            "hidden_source_row": bool(hidden),
            "internal_obj": None,
            "mapping_status": "unmatched",
        }
        equipment.append(equip)
        for month in range(1, 13):
            marker = _value(row, month + 5)
            if marker is None:
                continue
            normalized = str(marker).strip().upper().replace(" ", "")
            if normalized not in WORK_MARKERS:
                raise ValueError(f"TO row {row_number}, month {month}: unknown marker {marker!r}")
            work.append({
                "source_object_key": f"to:{ordinal}",
                "source_name": source_name,
                "equipment_source_row": row_number,
                "month": month,
                "work_type": normalized,
                "date_precision": "month",
                "hidden_source_row": bool(hidden),
                "internal_obj": None,
                "mapping_status": "unmatched",
            })
    return equipment, work


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def available_asof(available_from: dt.date, asof: dt.date) -> bool:
    """Historical scoring must not see a workbook received after the as-of day."""
    return asof >= available_from


def load_mapping(path: Path, schedule: dict) -> dict:
    """Reject a candidate mapping when its source workbook has changed."""
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if mapping.get("schema_version") != 1:
        raise ValueError("unsupported maintenance mapping schema")
    for name in ("ppr", "to"):
        actual = schedule["source_metadata"][name]["sha256"].lower()
        expected = mapping["source_sha256"][name].lower()
        if actual != expected:
            raise ValueError(f"{name} workbook hash changed; review object mapping")
    keys = set()
    source_keys = {row["source_object_key"] for row in schedule["ppr_objects"]}
    source_keys.update(row["source_object_key"] for row in schedule["to_equipment"])
    for entry in mapping["links"]:
        key = entry["source_object_key"]
        if key in keys:
            raise ValueError(f"duplicate maintenance mapping {key}")
        keys.add(key)
        if key not in source_keys:
            raise ValueError(f"maintenance mapping references unknown source key {key}")
        if entry["mapping_status"] not in ("inferred", "customer_confirmed"):
            raise ValueError(f"invalid mapping status for {key}")
        if entry["equipment_scope"] != "Газовый датчик":
            raise ValueError(f"unsupported equipment scope for {key}")
        if key.startswith("to:") and not any(
            row["source_object_key"] == key and row["equipment_type"] == "ГАСБ"
            for row in schedule["to_equipment"]
        ):
            raise ValueError(f"{key} has no GASB equipment row")
    return mapping


def maintenance_context(schedule: dict, mapping: dict, *, obj_parent: str | None,
                        sensor_type: str | None, asof: dt.date,
                        window_start: dt.date, window_end: dt.date) -> dict:
    """Return schedule context for a forecast without changing its risk or rank.

    `window_end` is inclusive. A TO marker means *some time in that month*,
    never confirmed work on the alert's particular day. Candidate links remain
    explicitly inferred, so consumers must not use them for suppression.
    """
    if window_end < window_start:
        raise ValueError("forecast window ends before it starts")
    first_known = max(dt.date.fromisoformat(schedule["available_from"]),
                      dt.date.fromisoformat(mapping["available_from"]))
    if not available_asof(first_known, asof):
        return {"status": "unavailable_asof", "available_from": first_known.isoformat(),
                "matches": []}
    schedule_year = start_year(schedule)
    if window_start.year != schedule_year or window_end.year != schedule_year:
        return {"status": "outside_schedule_year", "schedule_year": schedule_year,
                "matches": []}
    if sensor_type != "Газовый датчик":
        return {"status": "outside_equipment_scope", "matches": []}
    links = [x for x in mapping["links"] if str(x["obj_parent"]) == str(obj_parent)]
    if not links:
        return {"status": "unmapped", "matches": []}
    matches = []
    for link in links:
        key = link["source_object_key"]
        base = {"source_object_key": key, "obj_parent": str(obj_parent),
                "mapping_status": link["mapping_status"],
                "source": key.split(":", 1)[0]}
        if key.startswith("ppr:"):
            for row in schedule["ppr_objects"]:
                if row["source_object_key"] != key or not row["planned_dismantle"]:
                    continue
                start = dt.date.fromisoformat(row["planned_dismantle"])
                # A missing commission date does not justify guessing an end.
                end = dt.date.fromisoformat(row["planned_commission"]) if row["planned_commission"] else start
                if start <= window_end and end >= window_start:
                    matches.append({**base, "kind": "planned_ppr_window",
                                    "date_precision": "day", "planned_start": start.isoformat(),
                                    "planned_end": end.isoformat(),
                                    "end_known": row["planned_commission"] is not None})
        elif key.startswith("to:"):
            equipment_rows = {r["source_row"] for r in schedule["to_equipment"]
                              if r["source_object_key"] == key and r["equipment_type"] == "ГАСБ"}
            for row in schedule["to_work_months"]:
                if row["source_object_key"] != key or row["equipment_source_row"] not in equipment_rows:
                    continue
                month = row["month"]
                month_start = dt.date(window_start.year, month, 1)
                # Each schedule is a single calendar year; reject cross-year forecasts.
                next_month = (dt.date(month_start.year + 1, 1, 1) if month == 12
                              else dt.date(month_start.year, month + 1, 1))
                month_end = next_month - dt.timedelta(days=1)
                if month_start <= window_end and month_end >= window_start:
                    matches.append({**base, "kind": "planned_to_month",
                                    "date_precision": "month", "month": month,
                                    "work_type": row["work_type"],
                                    "equipment_source_row": row["equipment_source_row"]})
    return {"status": "schedule_overlap_unconfirmed" if matches else "no_planned_overlap",
            "matches": matches}


def start_year(schedule: dict) -> int:
    """Year of this annual maintenance snapshot, from its dated PPR records."""
    years = {int(row["planned_dismantle"][:4]) for row in schedule["ppr_objects"]
             if row["planned_dismantle"]}
    if len(years) != 1:
        raise ValueError("maintenance schedule must cover exactly one year")
    return years.pop()
