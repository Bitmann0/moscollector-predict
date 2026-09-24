"""Audit the revised channel/state catalogs against the full-history value counts.

The journal itself need not be scanned again: global_values.json contains exact
counts grouped by sensor type, raw value and alarm flag from the prior full pass.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_numeric(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except (InvalidOperation, ValueError):
        return False


def audit(
    old_channels_path: Path,
    new_channels_path: Path,
    objects_path: Path,
    states_path: Path,
    global_values_path: Path,
) -> dict:
    old_channels = rows(old_channels_path)
    new_channels = rows(new_channels_path)
    objects = rows(objects_path)
    states = rows(states_path)
    global_values = json.loads(global_values_path.read_text(encoding="utf-8"))

    old_by_id = {row["ид_канала_данных"]: row for row in old_channels}
    new_by_id = {row["ид_канала_данных"]: row for row in new_channels}
    object_by_id = {row["ид_объект"]: row for row in objects}
    object_counts = Counter(row["ид_объект"] for row in new_channels)
    existing_ids = old_by_id.keys() & new_by_id.keys()
    changed_fields = {
        field: sum(old_by_id[key][field] != new_by_id[key][field] for key in existing_ids)
        for field in old_channels[0]
    }

    state_flags: dict[tuple[str, str], set[bool]] = defaultdict(set)
    state_sets: dict[str, set[str]] = defaultdict(set)
    exact_state_rows = Counter(tuple(row.items()) for row in states)
    for row in states:
        state_flags[(row["тип_датчика"], row["название_состояния"])].add(
            row["тревожное"].lower() == "true"
        )
        state_sets[row["тип_датчика"]].add(row["ид_набор_состояний"])

    totals = Counter()
    per_type: dict[str, Counter] = defaultdict(Counter)
    unknown_values = Counter()
    alarm_disagreements = Counter()
    ambiguous_values = Counter()
    for row in global_values:
        sensor_type = row["sensor_type"]
        value = row["sensor_value"]
        count = int(row["n"])
        alarm = bool(row["alarm"])
        bucket = per_type[sensor_type or "<unknown channel type>"]
        totals["journal_rows"] += count
        bucket["journal_rows"] += count
        if sensor_type is None:
            totals["unknown_type_rows"] += count
            bucket["unknown_type_rows"] += count
            continue
        if is_numeric(value):
            totals["numeric_rows"] += count
            bucket["numeric_rows"] += count
            continue
        totals["text_rows"] += count
        bucket["text_rows"] += count
        flags = state_flags.get((sensor_type, value))
        if not flags:
            totals["unmatched_text_rows"] += count
            bucket["unmatched_text_rows"] += count
            unknown_values[(sensor_type, value, alarm)] += count
        elif len(flags) > 1:
            totals["ambiguous_flag_rows"] += count
            bucket["ambiguous_flag_rows"] += count
            ambiguous_values[(sensor_type, value, alarm)] += count
        else:
            totals["matched_text_rows"] += count
            bucket["matched_text_rows"] += count
            if alarm not in flags:
                totals["alarm_disagreement_rows"] += count
                bucket["alarm_disagreement_rows"] += count
                alarm_disagreements[(sensor_type, value, alarm, next(iter(flags)))] += count

    def top(counter: Counter, limit: int = 20) -> list[dict]:
        return [{"key": list(key), "rows": count} for key, count in counter.most_common(limit)]

    return {
        "source_sha256": {
            path.name: sha256(path)
            for path in (
                old_channels_path,
                new_channels_path,
                objects_path,
                states_path,
                global_values_path,
            )
            if path != old_channels_path
        }
        | {"old_channels_sha256": sha256(old_channels_path)},
        "channels": {
            "old_rows": len(old_channels),
            "new_rows": len(new_channels),
            "old_unique_ids": len(old_by_id),
            "new_unique_ids": len(new_by_id),
            "removed_ids": len(old_by_id.keys() - new_by_id.keys()),
            "added_ids": len(new_by_id.keys() - old_by_id.keys()),
            "changed_existing_fields": changed_fields,
            "object_ids_used": len(object_counts),
            "empty_object_id_channels": object_counts[""],
            "channels_with_unknown_object_id": sum(
                count for key, count in object_counts.items() if key not in object_by_id
            ),
            "object_catalog_rows": len(objects),
            "object_catalog_unique_ids": len(object_by_id),
            "object_levels": dict(Counter(row["иерархия_уровень"] for row in objects)),
            "objects_with_missing_parent": [
                row["ид_объект"]
                for row in objects
                if row["родитель"] and row["родитель"] not in object_by_id
            ],
            "used_objects_with_missing_parent": [
                row["ид_объект"]
                for row in objects
                if row["ид_объект"] in object_counts
                and row["родитель"]
                and row["родитель"] not in object_by_id
            ],
            "channel_types_without_state_entries": dict(
                Counter(
                    row["тип_датчика"]
                    for row in new_channels
                    if row["тип_датчика"] not in state_sets
                ).most_common()
            ),
        },
        "states": {
            "rows": len(states),
            "unique_exact_rows": len(exact_state_rows),
            "exact_duplicate_rows": sum(count - 1 for count in exact_state_rows.values()),
            "sensor_types": len(state_sets),
            "state_sets": len({row["ид_набор_состояний"] for row in states}),
            "sets_per_sensor_type": {key: sorted(value) for key, value in sorted(state_sets.items())},
            "type_value_pairs": len(state_flags),
            "conflicting_alarm_flags": [
                {"sensor_type": key[0], "value": key[1]}
                for key, flags in sorted(state_flags.items())
                if len(flags) > 1
            ],
            "has_numeric_threshold_column": any(
                "порог" in column.lower() for column in states[0]
            ),
        },
        "journal_value_coverage": {
            **dict(totals),
            "per_type": {key: dict(value) for key, value in sorted(per_type.items())},
            "top_unmatched_text_values": top(unknown_values),
            "top_alarm_disagreements": top(alarm_disagreements),
            "top_ambiguous_values": top(ambiguous_values),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-channels", type=Path, required=True)
    parser.add_argument("--new-channels", type=Path, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--states", type=Path, required=True)
    parser.add_argument("--global-values", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(
        args.old_channels,
        args.new_channels,
        args.objects,
        args.states,
        args.global_values,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
