"""Observed outcomes for issued recommendations, with explicit censoring.

An absent event is not a negative label unless the target could have been
observed. In particular, L9c needs the *next* report to reveal a gap's length;
the last report in a partial extract cannot be treated as a miss.
"""
from __future__ import annotations

import datetime as dt
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from statistics import median

import polars as pl

from .config import (EQUIPMENT_STYPES, EXCLUDED_PERIODS, MIN_FAILURE_DURATION_S,
                     PATHS)
from .guard_queue import BUILD_INFO, EVENT_DAYS
from .product_contract import OutcomeQuery, OutcomeResult


def _allowed(asof: dt.date, end: dt.date) -> bool:
    return not any(asof < stop and end >= start for start, stop in EXCLUDED_PERIODS)


def link_outcome(reports: list[dt.date], asof: dt.date,
                 last_day: dt.date | None) -> str:
    """L9c: next-day silence is known only after a later report resolves it."""
    target = asof + dt.timedelta(days=1)
    if last_day is None or target > last_day or not _allowed(asof, target):
        return "unknown"
    pos = bisect_left(reports, asof)
    if pos >= len(reports) or reports[pos] != asof:
        return "unknown"
    if pos + 1 >= len(reports):
        return "unknown"
    next_day = reports[pos + 1]
    if next_day == target:
        return "miss"
    # Same as _silence_rhythm_sql: count and median at the last report before
    # the gap, with a 30-day RANGE including its left boundary.
    left = asof - dt.timedelta(days=30)
    first = bisect_left(reports, left)
    recent = reports[first:pos + 1]
    if len(recent) < 7:
        return "miss"
    gaps = [(reports[i] - reports[i - 1]).days
            for i in range(first, pos + 1) if i > 0]
    usual = median(gaps) if gaps else 1
    length = (next_day - asof).days
    return "hit" if length >= 2 and length > 1.5 * usual else "miss"


def wear_outcome(observed: dict[dt.date, tuple[int | None, int | None]],
                 episode_starts: set[dt.date], asof: dt.date,
                 last_day: dt.date | None) -> str:
    """Recorded wear event wins; otherwise seven channel-days prove a miss."""
    end = asof + dt.timedelta(days=7)
    if not _allowed(asof, end):
        return "unknown"
    days = [asof + dt.timedelta(days=i) for i in range(1, 8)]
    if any(day in episode_starts or
           (day in observed and any(v is not None and v > 0
                                    for v in observed[day])) for day in days):
        return "hit"
    if last_day is None or end > last_day:
        return "unknown"
    if any(day not in observed or any(v is None for v in observed[day])
           for day in days):
        return "unknown"
    return "miss"


def weekly_outcome(obj: str, asof: dt.date,
                   positive: set[tuple[str, dt.date]],
                   unresolved: set[tuple[str, dt.date]],
                   observed: set[tuple[str, dt.date]],
                   last_day: dt.date | None) -> str:
    days = [asof + dt.timedelta(days=i) for i in range(2, 9)]
    if any((obj, day) in positive for day in days):
        return "hit"
    if last_day is None or days[-1] > last_day:
        return "unknown"
    if any((obj, day) not in observed or (obj, day) in unresolved
           for day in days):
        return "unknown"
    return "miss"


def _daily(items: list[OutcomeQuery]) -> tuple[dict[int, list[dt.date]], dict, dt.date | None]:
    path = PATHS.interim / "daily_channel.parquet"
    if not path.exists():
        return {}, {}, None
    last_day = pl.scan_parquet(path).select(pl.col("day").max()).collect().item()
    channels = sorted({item.channel for item in items if item.channel is not None})
    if not channels:
        return {}, {}, last_day
    rows = (pl.scan_parquet(path).filter(pl.col("ch").is_in(channels))
            .select("ch", "day", "n_alarms", "n_bad").collect()
            .sort(["ch", "day"]))
    dates: dict[int, list[dt.date]] = defaultdict(list)
    wear: dict[int, dict] = defaultdict(dict)
    for ch, day, alarms, bad in rows.iter_rows():
        dates[int(ch)].append(day)
        wear[int(ch)][day] = (alarms, bad)
    return dates, wear, last_day


def _episode_starts(items: list[OutcomeQuery]) -> dict[int, set[dt.date]]:
    path = PATHS.interim / "episodes.parquet"
    channels = sorted({item.channel for item in items
                       if item.head == "D" and item.channel is not None})
    if not path.exists() or not channels:
        return {}
    start = min(item.asof for item in items if item.head == "D")
    end = max(item.asof for item in items if item.head == "D") + dt.timedelta(days=7)
    rows = (pl.scan_parquet(path)
            .filter(pl.col("ch").is_in(channels) &
                    pl.col("t_start").dt.date().is_between(start, end) &
                    (pl.col("dur_s") >= MIN_FAILURE_DURATION_S) &
                    pl.col("stype").is_in(EQUIPMENT_STYPES))
            .select("ch", pl.col("t_start").dt.date().alias("day")).collect())
    out: dict[int, set[dt.date]] = defaultdict(set)
    for ch, day in rows.iter_rows():
        out[int(ch)].add(day)
    return out


def _weekly_data(items: list[OutcomeQuery]) -> tuple[set, set, set, dt.date | None]:
    path = PATHS.features / "object.parquet"
    if not path.exists() or not EVENT_DAYS.exists() or not BUILD_INFO.exists():
        return set(), set(), set(), None
    try:
        info = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
        if info.get("version") != 2:
            return set(), set(), set(), None
        cache_through = dt.date.fromisoformat(info["end"]) + dt.timedelta(days=1)
    except (OSError, ValueError, KeyError, TypeError):
        return set(), set(), set(), None
    weekly = [item for item in items if item.kind == "weekly_recommendation"]
    if not weekly:
        return set(), set(), set(), None
    objects = sorted({item.obj for item in weekly if item.obj is not None})
    if not objects:
        return set(), set(), set(), None
    start = min(item.asof for item in weekly) + dt.timedelta(days=2)
    end = max(item.asof for item in weekly) + dt.timedelta(days=8)
    frame = (pl.scan_parquet(path).filter(pl.col("obj").is_in(objects) &
            pl.col("day").is_between(start, end)).select("obj", "day").collect())
    feature_last_day = pl.scan_parquet(path).select(pl.col("day").max()).collect().item()
    last_day = min(feature_last_day, cache_through) if feature_last_day else None
    observed = {(str(obj), day) for obj, day in frame.iter_rows()}
    events = (pl.scan_parquet(EVENT_DAYS).filter(pl.col("obj").is_in(objects) &
              pl.col("day").is_between(start, end))
              .select("obj", "day", "positive", "unresolved").collect())
    positive = {(str(obj), day) for obj, day, hit, _ in events.iter_rows() if hit}
    unresolved = {(str(obj), day) for obj, day, _, bad in events.iter_rows() if bad}
    return positive, unresolved, observed, last_day


def resolve(items: list[OutcomeQuery]) -> list[OutcomeResult]:
    """Batch one read per source and preserve request order and unknowns."""
    if not items:
        return []
    channel_items = [item for item in items if item.kind == "alert"]
    dates, daily, last_channel_day = _daily(channel_items) if channel_items else ({}, {}, None)
    episodes = _episode_starts(channel_items) if channel_items else {}
    episodes_available = (PATHS.interim / "episodes.parquet").exists()
    positive, unresolved, observed, last_object_day = _weekly_data(items)
    out = []
    for item in items:
        value = "unknown"
        if item.kind == "alert" and item.channel is not None:
            if item.head == "A_link":
                value = link_outcome(dates.get(item.channel, []), item.asof,
                                     last_channel_day)
            elif item.head == "D" and episodes_available:
                value = wear_outcome(daily.get(item.channel, {}),
                                     episodes.get(item.channel, set()),
                                     item.asof, last_channel_day)
        elif item.kind == "weekly_recommendation" and item.obj is not None:
            value = weekly_outcome(item.obj, item.asof, positive, unresolved,
                                   observed, last_object_day)
        out.append(OutcomeResult(id=item.id, outcome=value))
    return out
