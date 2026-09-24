"""Audit unique episodes and repeat burden of weekly guarded-alarm policy."""
import datetime as dt
import json
from collections import Counter, defaultdict

import polars as pl

from mkl import guard_queue, guard_weekly, store
from mkl.config import PATHS

START = dt.date(2023, 2, 1)
END = dt.date(2026, 6, 22)
PERIODS = ((dt.date(2023, 2, 1), dt.date(2023, 12, 31)),
           (dt.date(2024, 1, 1), dt.date(2024, 12, 31)),
           (dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
           (dt.date(2026, 1, 1), END))


def main() -> None:
    all_base = store.read_slice("object", START, END, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    event = pl.read_parquet(guard_queue.EVENT_DAYS)
    base = guard_queue.current_candidates(all_base).filter(pl.col("day").dt.weekday() == 1)
    frame = guard_queue.priority_score(guard_queue.add_alarm_history(base, event))
    by_day = {day: block.sort(["priority_score", "obj"], descending=[True, False])
              for day, block in frame.partition_by("day", as_dict=True,
                                                   maintain_order=True).items()}
    # Polars partition_by(as_dict=True) uses tuple keys in some versions.
    by_day = {day[0] if isinstance(day, tuple) else day: block
              for day, block in by_day.items()}
    positive = defaultdict(list)
    unresolved = set()
    for obj, day, pos, unk in event.select("obj", "day", "positive", "unresolved").iter_rows():
        if pos:
            positive[str(obj)].append(day)
        if unk:
            unresolved.add((str(obj), day))
    episode = {}
    for obj, dates in positive.items():
        dates.sort()
        current = -1
        prior = None
        for day in dates:
            if prior is None or (day-prior).days > 7:
                current += 1
            episode[(obj, day)] = current
            prior = day
    observed = {(str(obj), day) for obj, day in pl.read_parquet(
        PATHS.features / "object.parquet", columns=["obj", "day"]).iter_rows()}

    def outcome(obj, day):
        future = [x for x in positive[obj] if day+dt.timedelta(days=2) <= x <=
                  day+dt.timedelta(days=8)]
        if future:
            return True, True, (obj, episode[(obj, future[0])])
        known = all((obj, day+dt.timedelta(days=i)) in observed and
                    (obj, day+dt.timedelta(days=i)) not in unresolved
                    for i in range(2, 9))
        return False, known, None

    all_positive_by_period = {}
    all_candidates_by_period = {}
    for start, end in PERIODS:
        key = str(start)
        sample = frame.filter(pl.col("day").is_between(start, end))
        all_candidates_by_period[key] = sample.height
        all_positive_by_period[key] = sum(outcome(str(obj), day)[0]
                                          for obj, day in sample.select("obj", "day").iter_rows())

    result = {"target": "recorded guarded alarm in days D+2..D+8",
              "reports": []}
    for min_week, cooldown in ((3, 0), (3, 14), (3, 28),
                               (4, 0), (4, 14), (4, 28)):
        last_selected = {}
        picks = []
        for day in sorted(by_day):
            chosen = 0
            for row in by_day[day].to_dicts():
                if row["exact_count_7"] < min_week:
                    continue
                obj = str(row["obj"])
                if cooldown and obj in last_selected and (day-last_selected[obj]).days <= cooldown:
                    continue
                hit, known, case = outcome(obj, day)
                picks.append({"obj": obj, "day": day, "hit": hit,
                              "known": known, "case": case})
                last_selected[obj] = day
                chosen += 1
                if chosen == 4:
                    break
        periods = []
        for start, end in PERIODS:
            part = [p for p in picks if start <= p["day"] <= end]
            n = len(part)
            hits = sum(p["hit"] for p in part)
            periods.append({"start": str(start), "end": str(end),
                            "alerts": n, "hits": hits,
                            "unknown": sum(not p["known"] for p in part),
                            "hit_rate": hits/n if n else None,
                            "eligible_candidate_weeks": all_candidates_by_period[str(start)],
                            "eligible_positive_weeks": all_positive_by_period[str(start)],
                            "recorded_recall": hits/all_positive_by_period[str(start)]
                            if all_positive_by_period[str(start)] else None,
                            "unique_objects": len({p["obj"] for p in part}),
                            "unique_hit_episodes": len({p["case"] for p in part if p["case"]})})
        n = len(picks)
        hits = sum(p["hit"] for p in picks)
        result["reports"].append({"min_alarm_days_in_last_7": min_week,
            "cooldown_days": cooldown,
            "periods": periods,
            "pooled": {"alerts": n, "hits": hits,
                       "hit_rate": hits/n if n else None,
                       "eligible_positive_weeks": sum(all_positive_by_period.values()),
                       "recorded_recall": hits/sum(all_positive_by_period.values()),
                       "unknown": sum(not p["known"] for p in picks),
                       "unique_objects": len({p["obj"] for p in picks}),
                       "unique_hit_episodes": len({p["case"] for p in picks if p["case"]}),
                       "top_object_alerts": Counter(p["obj"] for p in picks).most_common(10)}})
        if min_week == guard_weekly.MIN_ALARM_DAYS and cooldown == guard_weekly.COOLDOWN_DAYS:
            served, _ = guard_weekly.replay(all_base, event, END)
            actual = {(str(p["obj"]), p["day"]) for p in served}
            expected = {(p["obj"], p["day"]) for p in picks}
            if actual != expected:
                raise AssertionError(f"service differs from backtest: {len(actual)} vs {len(expected)}")
            result["service_replay_matches_backtest"] = True
    path = PATHS.reports / "guard_weekly_repeats.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in result["reports"]:
        print(row["min_alarm_days_in_last_7"], row["cooldown_days"], row["pooled"],
              [(p["alerts"], p["hits"]) for p in row["periods"]])


if __name__ == "__main__":
    main()
