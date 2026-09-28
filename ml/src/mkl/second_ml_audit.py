"""D/A_link audit. rule_head reuses build_outcomes and select_threshold to pick the production D threshold.

Outcomes carry their observation date. Dispatch selection never reads outcomes;
unknown results still consume budget. Equipment negatives require a report on
every future day (a conservative observation contract, not a physical-health label).
"""
from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from collections import defaultdict

import polars as pl

from . import serve
from .config import EQUIPMENT_STYPES, EXCLUDED_PERIODS, MIN_FAILURE_DURATION_S


def build_outcomes(con, head: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return candidate-day outcomes and actual signal/gap episode starts.

    L9c outcomes become available at the next report, not at day+1. For D,
    observed-normal days separate signal episodes; missing days do not invent
    new episodes. An onset without a normal report on the previous calendar
    day is explicitly uncertain. The source must contain unique (ch, day).
    """
    if head not in {"A_link", "D"}:
        raise ValueError("independent audit supports only D and A_link")
    if con.execute("SELECT count(*) FROM (SELECT ch, day FROM daily_channel "
                   "GROUP BY ch, day HAVING count(*) > 1)").fetchone()[0]:
        raise ValueError("daily_channel contains duplicate channel-days")
    if head == "A_link":
        outcomes = con.execute("""
          WITH gaps AS (
            SELECT ch, day, date_diff('day', lag(day) OVER w, day) AS gap
            FROM daily_channel WINDOW w AS (PARTITION BY ch ORDER BY day)
          ), rhythm AS (
            SELECT *, count(*) OVER w AS active_30,
                   median(gap) OVER w AS med_gap
            FROM gaps WINDOW w AS (PARTITION BY ch ORDER BY day
                   RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW)
          ), following AS (
            SELECT *, lead(day) OVER (PARTITION BY ch ORDER BY day) AS next_day
            FROM rhythm
          )
          SELECT ch, day,
            CASE WHEN next_day IS NULL THEN NULL ELSE CAST(
              date_diff('day', day, next_day) >= 2 AND active_30 >= 7 AND
              date_diff('day', day, next_day) > 1.5 * coalesce(med_gap, 1)
              AS INTEGER) END AS y,
            CAST(next_day AS DATE) AS available_on,
            CAST(day + INTERVAL 1 DAY AS DATE) AS event_day
          FROM following ORDER BY ch, day
        """).pl()
        events = outcomes.filter(pl.col("y") == 1).select(
            "ch", "event_day", "available_on").with_columns(
                pl.col("event_day").alias("last_signal_day"),
                pl.lit(True).alias("onset_observed"))
    else:
        eq = "(" + ",".join("'" + s + "'" for s in sorted(EQUIPMENT_STYPES)) + ")"
        # Aggregated duration uses a later observed BAD event. Without raw
        # timestamps we conservatively wait for that last observation; merely
        # advancing the clock by one hour does not establish the duration.
        con.execute(f"""
          CREATE OR REPLACE TEMP TABLE audit_signals AS
          SELECT ch, event_day, min(available_on) AS available_on FROM (
            SELECT ch, day AS event_day, day AS available_on FROM daily_channel
            WHERE stype IN {eq} AND (n_bad > 0 OR n_alarms > 0)
            UNION ALL
            SELECT ch, CAST(t_start AS DATE),
                   CAST(t_start + dur_s * INTERVAL 1 SECOND AS DATE)
            FROM episodes WHERE stype IN {eq} AND dur_s >= {MIN_FAILURE_DURATION_S}
          ) GROUP BY ch, event_day
        """)
        outcomes = con.execute(f"""
          WITH base AS (
            SELECT ch, day FROM daily_channel WHERE stype IN {eq}
          ), observed AS (
            SELECT b.ch, b.day, count(d.day) FILTER (
              WHERE d.n_bad IS NOT NULL AND d.n_alarms IS NOT NULL) AS future_days
            FROM base b LEFT JOIN daily_channel d ON b.ch = d.ch
              AND d.day > b.day AND d.day <= b.day + INTERVAL 7 DAY
            GROUP BY b.ch, b.day
          ), signals AS (
            SELECT b.ch, b.day, count(s.event_day) AS positives,
                   min(s.available_on) AS first_evidence
            FROM base b LEFT JOIN audit_signals s ON b.ch = s.ch
              AND s.event_day > b.day AND s.event_day <= b.day + INTERVAL 7 DAY
            GROUP BY b.ch, b.day
          )
          SELECT b.ch, b.day,
            CASE WHEN b.day + INTERVAL 7 DAY > (SELECT max(day) FROM daily_channel)
                 THEN NULL
                 WHEN s.positives > 0 THEN 1
                 WHEN o.future_days = 7 THEN 0 ELSE NULL END AS y,
            CAST(greatest(b.day + INTERVAL 7 DAY, s.first_evidence) AS DATE)
                 AS available_on,
            o.future_days AS observed_future_days
          FROM base b JOIN observed o USING (ch, day) JOIN signals s USING (ch, day)
          ORDER BY b.ch, b.day
        """).pl()
        # Runs are delimited by an observed clean day, never by a telemetry gap.
        events = con.execute(f"""
          WITH days AS (
            SELECT d.ch, d.day, s.event_day IS NOT NULL AS bad,
                   (coalesce(d.n_bad, 0) = 0 AND coalesce(d.n_alarms, 0) = 0
                    AND d.n_bad IS NOT NULL AND d.n_alarms IS NOT NULL
                    AND s.event_day IS NULL) AS clean, s.available_on
            FROM daily_channel d LEFT JOIN audit_signals s
              ON d.ch = s.ch AND d.day = s.event_day WHERE d.stype IN {eq}
            UNION ALL
            SELECT s.ch, s.event_day, TRUE, FALSE, s.available_on
            FROM audit_signals s WHERE NOT EXISTS (
              SELECT 1 FROM daily_channel d WHERE d.ch=s.ch AND d.day=s.event_day)
          ), marked AS (
            SELECT *, sum(CAST(clean AS INTEGER)) OVER w AS episode_group,
                   lag(day) OVER w AS prev_day, lag(clean) OVER w AS prev_clean
            FROM days WINDOW w AS (PARTITION BY ch ORDER BY day)
          )
          SELECT ch, min(day) AS event_day, max(day) AS last_signal_day,
                 arg_min(available_on, day) AS available_on,
                 coalesce(arg_min(prev_clean AND date_diff('day', prev_day, day)=1,
                                  day), FALSE) AS onset_observed
          FROM marked WHERE bad GROUP BY ch, episode_group ORDER BY ch, event_day
        """).pl()
    horizon = 1 if head == "A_link" else 7
    for start, end in EXCLUDED_PERIODS:
        outcomes = outcomes.with_columns(pl.when(
            (pl.col("day") + dt.timedelta(days=1) <= end) &
            (pl.col("day") + dt.timedelta(days=horizon) >= start)
        ).then(None).otherwise(pl.col("y")).alias("y"))
        events = events.filter(~pl.col("event_day").is_between(start, end))
    events = events.with_columns(pl.concat_str(
        [pl.col("ch").cast(pl.String), pl.col("event_day").cast(pl.String)],
        separator=":").alias("episode_id"))
    return outcomes, events


def labels_asof(outcomes: pl.DataFrame, asof: dt.date) -> pl.DataFrame:
    """Mask future evidence without removing candidates or changing their keys."""
    return outcomes.with_columns(pl.when(pl.col("available_on") <= asof)
                                 .then(pl.col("y")).otherwise(None).alias("y"))


def candidates(features: pl.DataFrame, outcomes: pl.DataFrame,
               head: str, asof: dt.date) -> pl.DataFrame:
    if head == "D":
        features = features.filter(pl.col("stype").is_in(EQUIPMENT_STYPES))
    elif head != "A_link":
        raise ValueError(head)
    if features.select(pl.struct("ch", "day").n_unique()).item() != features.height:
        raise ValueError("duplicate feature channel-days")
    return features.join(labels_asof(outcomes, asof).select("ch", "day", "y"),
                         on=["ch", "day"], how="left", validate="1:1").sort(
                             ["day", "obj", "ch"])


def daily_top(scores: pl.DataFrame, budget: int, per_object: bool) -> pl.DataFrame:
    if budget < 0:
        raise ValueError("budget must be non-negative")
    if scores.is_empty():
        return scores
    if not scores["risk"].is_finite().all() or scores["risk"].null_count():
        raise ValueError("scores must be finite")
    return pl.concat([serve._apply_budget(day, budget, per_object).filter(
        pl.col("alert")).drop("alert") for day in scores.partition_by(
            "day", maintain_order=True)]).sort(["day", "obj", "ch"])


def replay(top: pl.DataFrame, threshold: float | None, cooldown: int = 7,
           history: list[tuple[int, dt.date]] | None = None) -> pl.DataFrame:
    """Replay pre-ranked daily slots, with an explicit abstention state.

    None means no feasible policy, never an unbounded baseline threshold.
    Day-zero history is ignored to match the service's idempotent reruns.
    """
    if cooldown < 0:
        raise ValueError("cooldown must be non-negative")
    issued = defaultdict(list)
    for ch, day in history or []:
        issued[ch].append(day)
    flags = []
    for ch, day, risk in top.select("ch", "day", "risk").iter_rows():
        selected = (threshold is not None and risk >= threshold and
                    not any(0 < (day - prev).days <= cooldown for prev in issued[ch]))
        flags.append(selected)
        if selected:
            issued[ch].append(day)
        issued[ch] = [prev for prev in issued[ch] if (day-prev).days <= cooldown]
    return top.with_columns(pl.Series("alert", flags, dtype=pl.Boolean))


def select_threshold(top: pl.DataFrame, minimum: float, min_alerts: int = 30,
                     cooldown: int = 7) -> dict:
    """Exhaustive selection on complete issued-policy outcomes, including ties.

    At most budget*window_days distinct scores need replay. Unknowns contribute
    to the denominator only. Maximise known hits, then precision, then threshold.
    """
    if not 0 <= minimum <= 1 or min_alerts < 1:
        raise ValueError("invalid selection constraints")
    best = None
    for threshold in sorted(top["risk"].unique().to_list(), reverse=True):
        picked = replay(top, threshold, cooldown).filter(pl.col("alert"))
        n = picked.height
        hits = picked.filter(pl.col("y") == 1).height
        if n < min_alerts or hits / n < minimum:
            continue
        rank = (hits, hits / n, threshold)
        if best is None or rank > best[0]:
            best = (rank, {"feasible": True, "threshold": threshold,
                          "alerts": n, "hits": hits,
                          "unknown": picked["y"].null_count(),
                          "precision_lower_bound": hits / n})
    return best[1] if best else {"feasible": False, "threshold": None,
                                "reason": "no threshold satisfies full-policy gate"}


def summarize(all_candidates: pl.DataFrame, issued: pl.DataFrame,
              events: pl.DataFrame, start: dt.date, end: dt.date,
              horizon: int, reference_channels: int | None = None,
              reference_ids: set[int] | None = None) -> dict:
    """Evaluate exact event starts in (asof, asof+H], never starts of shifted y.

    A recorded episode is eligible when at least one scored candidate could
    predict its onset. An episode already underway is not a new caught onset.
    Alerts may cover multiple starts; repeats count only recommendations that
    add no new matched episode. Uncertain onsets are reported separately.
    """
    chosen = issued.filter(pl.col("alert") & pl.col("day").is_between(start, end))
    full = all_candidates.filter(pl.col("day").is_between(start, end))
    by_channel = defaultdict(list)
    for ch, day in full.select("ch", "day").iter_rows():
        by_channel[ch].append(day)
    for days in by_channel.values():
        days.sort()
    eligible = {}
    eligible_observed = set()
    for row in events.iter_rows(named=True):
        ch, event_day = row["ch"], row["event_day"]
        days = by_channel[ch]
        lo = bisect_left(days, event_day - dt.timedelta(days=horizon))
        hi = bisect_left(days, event_day)
        if hi > lo:
            eligible[row["episode_id"]] = row
            if row["onset_observed"]:
                eligible_observed.add(row["episode_id"])
    lookup = defaultdict(list)
    for row in events.sort("event_day").iter_rows(named=True):
        lookup[row["ch"]].append(row)
    event_days = {ch: [r["event_day"] for r in rows] for ch, rows in lookup.items()}
    caught, repeats, ongoing, new_alerts, uncertain_alerts = set(), 0, 0, 0, 0
    for ch, day in chosen.sort(["day", "ch"]).select("ch", "day").iter_rows():
        rows, days = lookup[ch], event_days.get(ch, [])
        lo, hi = bisect_right(days, day), bisect_right(days, day+dt.timedelta(days=horizon))
        matched = {r["episode_id"] for r in rows[lo:hi] if r["episode_id"] in eligible}
        if matched - caught:
            new_alerts += 1
        elif matched:
            repeats += 1
        if matched and not (matched & eligible_observed):
            uncertain_alerts += 1
        caught.update(matched)
        if lo and rows[lo-1]["event_day"] <= day < rows[lo-1]["last_signal_day"]:
            ongoing += 1
    n, hits, unknown = chosen.height, chosen.filter(pl.col("y") == 1).height, chosen["y"].null_count()
    positives = full.filter(pl.col("y") == 1).height
    daily_counts = dict(chosen.group_by("day").len().iter_rows())
    daily_candidates = dict(full.group_by("day").len().iter_rows())
    dates = [start+dt.timedelta(days=i) for i in range((end-start).days+1)]
    monthly = defaultdict(lambda: {"alerts": 0, "hits": 0, "unknown": 0})
    for day in dates:
        monthly[str(day)[:7]]  # Include zero-alert months.
    for row in chosen.iter_rows(named=True):
        counts = monthly[str(row["day"])[:7]]
        counts["alerts"] += 1
        counts["hits"] += row["y"] == 1
        counts["unknown"] += row["y"] is None
    scored_channels = full["ch"].n_unique()
    matched_channels = (len(set(full["ch"].to_list()) & reference_ids)
                        if reference_ids is not None else None)
    return {"candidates": full.height, "candidate_channels": scored_channels,
            "reference_channels": reference_channels,
            "channels_matched_to_current_catalog": matched_channels,
            "channels_outside_current_catalog": (scored_channels-matched_channels
                                                  if matched_channels is not None else None),
            "reference_coverage": (matched_channels / reference_channels
                                   if reference_channels and matched_channels is not None else None),
            "unknown_candidates": full["y"].null_count(), "known_positive_days": positives,
            "alerts": n, "hits": hits, "known_misses": n-hits-unknown, "unknown_alerts": unknown,
            "precision_lower_bound": hits/n if n else None,
            "precision_upper_bound": (hits+unknown)/n if n else None,
            "precision_known_only": hits/(n-unknown) if n > unknown else None,
            "recall_known_days": hits/positives if positives else None,
            "episodes_eligible": len(eligible), "episodes_caught": len(caught),
            "episode_recall": len(caught)/len(eligible) if eligible else None,
            "observed_onsets_eligible": len(eligible_observed),
            "observed_onsets_caught": len(caught & eligible_observed),
            "alerts_with_new_episode": new_alerts, "repeat_episode_alerts": repeats,
            "ongoing_episode_alerts": ongoing, "uncertain_onset_only_alerts": uncertain_alerts,
            "episodes_per_100_alerts": 100*len(caught)/n if n else None,
            "alerts_per_day": n/len(dates), "alerts_per_30_days": n/len(dates)*30,
            "days_without_alerts": sum(daily_counts.get(d, 0) == 0 for d in dates),
            "days_without_candidates": sum(daily_candidates.get(d, 0) == 0 for d in dates),
            "monthly": dict(sorted(monthly.items())),
            "daily": [{"day": str(d), "candidates": daily_candidates.get(d, 0),
                       "alerts": daily_counts.get(d, 0)} for d in dates]}
