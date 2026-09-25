"""Safe, source-aware intake of planned maintenance spreadsheets.

The two spreadsheets use local ordinal numbers, not catalog object IDs.  This
module deliberately does not create an automatic catalog join.
"""
from __future__ import annotations

import datetime as dt
import hashlib
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
    seen = set()
    for row_number, row, hidden in rows:
        block_number = _value(row, 1)
        if isinstance(block_number, int) and not isinstance(block_number, bool):
            if block_number in seen:
                raise ValueError(f"duplicate TO object ordinal {block_number}")
            seen.add(block_number)
            ordinal = block_number
        raw_type = _value(row, 3)
        quantity = _value(row, 4)
        if ordinal is None or not isinstance(raw_type, str):
            continue
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            continue  # repeated page header, not equipment
        equip = {
            "source_object_key": f"to:{ordinal}",
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
