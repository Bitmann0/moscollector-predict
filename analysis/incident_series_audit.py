"""Группы аварий и серии ППР/ТО на журнале стенда: замер для порогов в semantics.py.

Отвечает на три вопроса к ответам заказчика 28.09.2026 (analysis/qa_customer_2026-09-28.md):
сколько тревожных сообщений попадает в каждую группу аварий (ответ 2); в какие часы и
дни недели идут серии от 5 пожарных извещателей одного объекта за 10 минут — отсюда
«рабочее время» (ответ 5); какой порог числа газоанализаторов одного комплекса отделяет
проход вдоль коллектора от срабатывания в одном месте.

Группы и серии считают те же функции, что приём событий: incident_group и series_hints
из backend/app/services/semantics.py. Для распределения по часам проверка рабочего
времени отключена, для порогов газа — оставлена. Серия в отчёте — события с подсказкой
одного ключа подряд, без разрыва больше 10 минут; её время — время первого события.

Вход — выгрузка тревожных сообщений из БД сервиса, только SELECT; \\copy в psql
читается одной строкой (в Git Bash — с MSYS_NO_PATHCONV=1):

    docker exec mkinteg-db-1 psql -U moscollector -d moscollector -c "\\copy (select e.id, e.channel_id, to_char(e.ts at time zone 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS') ts, e.alarm, e.val_raw, c.sensor_type, c.obj_id, o.parent_id, c.picket from events e left join ref_channels c on c.id = e.channel_id left join ref_objects o on o.id = c.obj_id where e.alarm order by e.id) to stdout with csv header" > alarm_events.csv
    python analysis/incident_series_audit.py alarm_events.csv

Результат — analysis/incident_series_audit.json.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import semantics
from app.services.helpers import MSK

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["alarm"] == "t"]
    for row in rows:
        row["ts"] = datetime.fromisoformat(row["ts"]).replace(tzinfo=MSK)
        row["channel_id"] = int(row["channel_id"])
        row["group"] = semantics.incident_group(row["sensor_type"] or None, row["val_raw"])
    return rows


def series(rows: list[dict], group: str, *, working_time: bool) -> list[list[dict]]:
    """Серии группы: события с подсказкой одного ключа подряд, разрыв не больше 10 минут."""
    events = [semantics.SeriesEvent(ref=n, group=group,
                                    key=semantics.series_key(group, r["obj_id"] or None,
                                                             r["parent_id"] or None),
                                    channel_id=r["channel_id"], ts=r["ts"])
              for n, r in enumerate(rows) if r["group"] == group]
    original = semantics._working_time
    if not working_time:
        semantics._working_time = lambda ts: True
    try:
        hinted = semantics.series_hints(events)
    finally:
        semantics._working_time = original
    by_key: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if event.ref in hinted:
            by_key[event.key].append(rows[event.ref])
    out: list[list[dict]] = []
    for items in by_key.values():
        items.sort(key=lambda r: r["ts"])
        current = [items[0]]
        for row in items[1:]:
            if row["ts"] - current[-1]["ts"] > semantics.SERIES_WINDOW:
                out.append(current)
                current = []
            current.append(row)
        out.append(current)
    return sorted(out, key=lambda s: s[0]["ts"])


def describe(chain: list[dict], key: str) -> dict:
    start = chain[0]["ts"]
    return {"start": start.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[start.weekday()],
            key: chain[0][key], "channels": len({r["channel_id"] for r in chain}),
            "pickets": len({r["picket"] for r in chain}), "events": len(chain),
            "minutes": round((chain[-1]["ts"] - start).total_seconds() / 60, 1)}


def fire_report(rows: list[dict]) -> dict:
    chains = series(rows, "fire", working_time=False)
    starts = [c[0]["ts"] for c in chains]
    weekday_hours = Counter(t.hour for t in starts if t.weekday() < 5)
    windows = {f"будни {lo}:00–{hi - 1}:59": sum(1 for t in starts
                                                if t.weekday() < 5 and lo <= t.hour < hi)
               for lo, hi in ((8, 14), (9, 18), (8, 18), (8, 20))}
    hinted = series(rows, "fire", working_time=True)
    return {
        "threshold": semantics.SERIES_MIN["fire"],
        "events_in_group": sum(1 for r in rows if r["group"] == "fire"),
        "series_any_time": len(chains),
        "series_by_weekday": {WEEKDAYS[d]: n for d, n in
                              sorted(Counter(t.weekday() for t in starts).items())},
        "series_by_start_hour_weekdays": dict(sorted(weekday_hours.items())),
        "series_by_start_hour_weekend": dict(sorted(
            Counter(t.hour for t in starts if t.weekday() >= 5).items())),
        "series_starting_in_window": windows,
        "outside_working_time": [describe(c, "obj_id") for c in chains
                                 if not semantics._working_time(c[0]["ts"])],
        "with_working_time_rule": {"series": len(hinted),
                                   "events": sum(len(c) for c in hinted)},
        "series": [describe(c, "obj_id") for c in chains],
    }


def gas_report(rows: list[dict]) -> dict:
    original = dict(semantics.SERIES_MIN)
    table = []
    try:
        for threshold in (2, 3, 4, 5, 6):
            semantics.SERIES_MIN["gas"] = threshold
            chains = series(rows, "gas", working_time=True)
            table.append({
                "threshold": threshold, "series": len(chains),
                "events": sum(len(c) for c in chains),
                "series_on_fewer_pickets_than_threshold": sum(
                    1 for c in chains if len({r["picket"] for r in c}) < threshold),
                "series_on_at_most_two_pickets": sum(
                    1 for c in chains if len({r["picket"] for r in c}) <= 2),
            })
    finally:
        semantics.SERIES_MIN.update(original)
    chains = series(rows, "gas", working_time=True)
    any_time = series(rows, "gas", working_time=False)
    return {"threshold": semantics.SERIES_MIN["gas"],
            "events_in_group": sum(1 for r in rows if r["group"] == "gas"),
            "by_threshold": table,
            "series_any_time": len(any_time),
            "outside_working_time": [describe(c, "parent_id") for c in any_time
                                     if not semantics._working_time(c[0]["ts"])],
            "series": [describe(c, "parent_id") for c in chains]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("journal", type=Path, help="CSV из запроса в шапке модуля")
    parser.add_argument("--out", type=Path, default=ROOT / "analysis" / "incident_series_audit.json")
    args = parser.parse_args()
    rows = load(args.journal)
    groups = Counter((r["group"], r["sensor_type"], r["val_raw"]) for r in rows if r["group"])
    report = {
        "source": {"file": args.journal.name,
                   "sha256": hashlib.sha256(args.journal.read_bytes()).hexdigest(),
                   "alarm_rows": len(rows),
                   "first": min(r["ts"] for r in rows).isoformat(),
                   "last": max(r["ts"] for r in rows).isoformat()},
        "groups": dict(Counter(r["group"] for r in rows if r["group"]).most_common()),
        "without_group": sum(1 for r in rows if not r["group"]),
        "group_detail": [{"group": g, "sensor_type": s, "value": v, "events": n}
                         for (g, s, v), n in sorted(groups.items(), key=lambda x: (x[0][0], -x[1]))],
        "fire": fire_report(rows),
        "gas": gas_report(rows),
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("source", "groups", "without_group")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
