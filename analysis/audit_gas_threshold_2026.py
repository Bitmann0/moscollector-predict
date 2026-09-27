"""Compare the customer's 1% methane threshold with gas text events in 2026.

This is a descriptive audit, not a confirmed-incident label. The input journal
is streamed once; no in-memory copy of the 30-million-row file is needed.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def seconds(time: str) -> int:
    hour, minute, second = map(int, time.split(":"))
    return hour * 3600 + minute * 60 + second


def audit(channels_path: Path, journal_path: Path, max_rows: int | None = None) -> dict:
    with channels_path.open(encoding="utf-8-sig", newline="") as handle:
        gas_channels = {
            row["ид_канала_данных"]: row["ид_объект"]
            for row in csv.DictReader(handle)
            if row["тип_датчика"] == "Газовый датчик"
        }

    counts = Counter()
    threshold_by_day: dict[tuple[str, str], list[int]] = defaultdict(list)
    gas_text_by_day: dict[tuple[str, str], list[int]] = defaultdict(list)
    threshold_objects = Counter()
    gas_text_objects = Counter()
    threshold_hours = Counter()
    gas_text_hours = Counter()
    with journal_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        expected = [
            "ид_события", "ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"
        ]
        if header != expected:
            raise ValueError(f"Unexpected journal columns: {header}")
        for row in reader:
            counts["journal_rows"] += 1
            if max_rows is not None and counts["journal_rows"] > max_rows:
                counts["journal_rows"] -= 1
                break
            if len(row) != 6 or row[1] not in gas_channels:
                continue
            counts["gas_rows"] += 1
            channel, day, time, alarm, value = row[1], row[2], row[3], row[4], row[5]
            key = (channel, day)
            if value == "Обнаружен газ":
                counts["gas_text_rows"] += 1
                counts["gas_text_alarm_true_rows"] += alarm.lower() in ("t", "true")
                gas_text_by_day[key].append(seconds(time))
                gas_text_objects[gas_channels[channel]] += 1
                gas_text_hours[time[:2]] += 1
                continue
            try:
                number = float(value)
            except ValueError:
                continue
            counts["numeric_gas_rows"] += 1
            if 1 <= number < 327.68:
                counts["numeric_gas_ge_1_rows"] += 1
                counts["numeric_gas_ge_1_alarm_true_rows"] += alarm.lower() in ("t", "true")
                threshold_by_day[key].append(seconds(time))
                threshold_objects[gas_channels[channel]] += 1
                threshold_hours[time[:2]] += 1

    both_days = threshold_by_day.keys() & gas_text_by_day.keys()
    close_5m = close_1h = 0
    for key in both_days:
        alerts = sorted(gas_text_by_day[key])
        closest = min(
            min(
                (abs(t - alerts[index]) for index in (bisect.bisect_left(alerts, t) - 1, bisect.bisect_left(alerts, t)) if 0 <= index < len(alerts)),
                default=86400,
            )
            for t in threshold_by_day[key]
        )
        close_5m += closest <= 300
        close_1h += closest <= 3600

    return {
        "source": {
            "journal": journal_path.name,
            "journal_bytes": journal_path.stat().st_size,
            "channels_sha256": hashlib.sha256(channels_path.read_bytes()).hexdigest(),
            "max_rows": max_rows,
        },
        "counts": dict(counts),
        "gas_channels": len(gas_channels),
        "numeric_threshold_channel_days": len(threshold_by_day),
        "gas_text_channel_days": len(gas_text_by_day),
        "both_on_same_channel_day": len(both_days),
        "both_within_5_minutes_on_same_day": close_5m,
        "both_within_1_hour_on_same_day": close_1h,
        "objects_with_numeric_threshold": len(threshold_objects),
        "objects_with_gas_text": len(gas_text_objects),
        "numeric_threshold_hours": dict(sorted(threshold_hours.items())),
        "gas_text_hours": dict(sorted(gas_text_hours.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-rows", type=int)
    args = parser.parse_args()
    result = audit(args.channels, args.journal, args.max_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("counts", "numeric_threshold_channel_days", "gas_text_channel_days", "both_on_same_channel_day", "both_within_1_hour_on_same_day")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
