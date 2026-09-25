"""As-of-safe A_link monitoring with optional planned-maintenance context.

The target remains the recorded L9c telemetry gap. Planned-work overlaps are
reported separately; an Excel plan is never treated as a confirmed work event.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

import polars as pl

from mkl import serve
from mkl.maintenance import maintenance_context


def _outcomes(rows: list[dict], positives: int | None = None) -> dict:
    alerts = len(rows)
    hits = sum(row["y"] == 1 for row in rows)
    unknown = sum(row["y"] is None for row in rows)
    return {
        "alerts": alerts, "proxy_hits": hits,
        "known_misses": alerts - hits - unknown, "unknown": unknown,
        "precision_lower_bound": hits / alerts if alerts else None,
        "precision_known_only": hits / (alerts - unknown) if alerts > unknown else None,
        "recall_known": hits / positives if positives else None,
    }


def _review_metrics(rows: list[dict], reviews: pl.DataFrame | None) -> dict | None:
    """Human-adjudicated cause among *issued alerts*, if such labels exist."""
    if reviews is None:
        return None
    required = {"ch", "day", "verdict"}
    if not required.issubset(reviews.columns):
        raise ValueError(f"review rows missing {sorted(required - set(reviews.columns))}")
    if reviews.select(pl.struct("ch", "day").n_unique()).item() != reviews.height:
        raise ValueError("duplicate review for channel/day")
    allowed = {"confirmed_planned_cause", "confirmed_unrelated", "unknown", None}
    if not set(reviews["verdict"].to_list()).issubset(allowed):
        raise ValueError("invalid maintenance review verdict")
    lookup = {(r["ch"], r["day"]): r["verdict"] for r in reviews.to_dicts()}
    confirmed = 0
    flagged_confirmed = 0
    flagged_reviewed = 0
    flagged = 0
    reviewed = 0
    for row in rows:
        verdict = lookup.get((row["ch"], row["day"]))
        known = verdict in {"confirmed_planned_cause", "confirmed_unrelated"}
        marked = row["context_status"] == "schedule_overlap_unconfirmed"
        reviewed += known
        confirmed += verdict == "confirmed_planned_cause"
        if marked:
            flagged += 1
            flagged_reviewed += known
            flagged_confirmed += verdict == "confirmed_planned_cause"
    return {
        "issued_alerts": len(rows), "reviewed_alerts": reviewed,
        "flagged_alerts": flagged, "flagged_reviewed": flagged_reviewed,
        "flagged_confirmed_planned_causes": flagged_confirmed,
        "confirmed_planned_causes_all_issued": confirmed,
        "flag_precision_lower_bound": flagged_confirmed / flagged
        if flagged and flagged_reviewed else None,
        "flag_precision_known_only": flagged_confirmed / flagged_reviewed
        if flagged_reviewed else None,
        "flag_recall_among_reviewed_issued": flagged_confirmed / confirmed
        if confirmed else None,
    }


def evaluate_link_schedule(scored: pl.DataFrame, channels: pl.DataFrame,
                           schedule: dict, mapping: dict, *, threshold: float,
                           labels_mature_through: dt.date | None = None,
                           reviews: pl.DataFrame | None = None) -> dict:
    """Replay frozen A_link policy and separate retro audit from valid future days.

    `scored` needs ch/day/risk/y, with null y for unresolved outcomes. For a
    genuine online evaluation the scored file must include the prior seven
    days or be replaced by the backend's durable issued-alert journal.
    """
    required = {"ch", "day", "risk", "y"}
    if not required.issubset(scored.columns):
        raise ValueError(f"scored rows missing {sorted(required - set(scored.columns))}")
    if scored.is_empty():
        raise ValueError("no scored rows")
    if channels.select(pl.col("ch").n_unique()).item() != channels.height:
        raise ValueError("catalog has duplicate channel IDs")
    first_known = max(dt.date.fromisoformat(schedule["available_from"]),
                      dt.date.fromisoformat(mapping["available_from"]))
    ppr_keys = {x["source_object_key"] for x in mapping["links"]
                if x["source_object_key"].startswith("ppr:")}
    to_keys = {x["source_object_key"] for x in mapping["links"]
               if x["source_object_key"].startswith("to:")}
    gas_catalog = channels.filter(pl.col("stype") == "Газовый датчик")
    mapped_parents = {str(x["obj_parent"]) for x in mapping["links"]}
    future_ppr = [r for r in schedule["ppr_objects"]
                  if r["planned_dismantle"]
                  and dt.date.fromisoformat(r["planned_dismantle"]) >= first_known]
    to_object_count = len({r["source_object_key"] for r in schedule["to_equipment"]})
    chosen = serve.alerts_over_time(
        scored.sort(["day", "ch"]), 20, "ch", cooldown_days=7,
        threshold=threshold).filter(pl.col("alert"))
    address = channels.select("ch", "obj_parent", "stype")
    chosen = chosen.join(address, on="ch", how="left")
    rows = []
    for row in chosen.select("ch", "day", "y", "obj_parent", "stype").to_dicts():
        day = row["day"]
        historical = day < first_known
        # In the historical audit only, pretend the schedule is available to
        # inspect overlap. This branch NEVER enters prospective policy metrics.
        context = maintenance_context(
            schedule, mapping, obj_parent=row["obj_parent"],
            sensor_type=row["stype"], asof=first_known if historical else day,
            window_start=day + dt.timedelta(days=1),
            window_end=day + dt.timedelta(days=1))
        rows.append({**row, "historical": historical,
                     "context_status": context["status"],
                     "schedule_matches": context["matches"]})

    historical_rows = [r for r in rows if r["historical"]]
    future_rows = [r for r in rows if not r["historical"]]
    historical_positives = scored.filter((pl.col("day") < first_known)
                                         & (pl.col("y") == 1)).height
    mature_rows = ([r for r in future_rows
                    if r["day"] + dt.timedelta(days=1) <= labels_mature_through]
                   if labels_mature_through else [])
    candidates = (scored.filter((pl.col("day") >= first_known)
                                & (pl.col("day") + dt.timedelta(days=1)
                                   <= labels_mature_through))
                  if labels_mature_through else scored.head(0))
    positives = candidates.filter(pl.col("y") == 1).height

    def context_counts(items: list[dict]) -> dict:
        return dict(sorted(Counter(r["context_status"] for r in items).items()))

    retro_overlap = [r for r in historical_rows
                     if r["context_status"] == "schedule_overlap_unconfirmed"]
    future_overlap = [r for r in future_rows
                      if r["context_status"] == "schedule_overlap_unconfirmed"]
    review_metrics = _review_metrics(future_rows, reviews)
    if not future_rows:
        status = "awaiting_post_release_predictions"
    elif labels_mature_through is None:
        status = "label_maturity_not_declared"
    elif not mature_rows:
        status = "awaiting_mature_labels"
    elif not any(r["y"] is not None for r in mature_rows):
        status = "awaiting_observed_outcomes"
    else:
        status = "descriptive_proxy_metrics_available"

    return {
        "evaluation_target": "A_link_L9c_recorded_gap_not_physical_failure",
        "schedule_available_from": schedule["available_from"],
        "mapping_available_from": mapping["available_from"],
        "last_scored_day": str(scored["day"].max()),
        "mapping_coverage": {
            "ppr_objects_mapped_candidates": len(ppr_keys),
            "ppr_objects_total": len(schedule["ppr_objects"]),
            "to_objects_mapped_candidates": len(to_keys),
            "to_objects_total": to_object_count,
            "catalog_gas_channels_mapped_candidates": gas_catalog.filter(
                pl.col("obj_parent").is_in(mapped_parents)).height,
            "catalog_gas_channels_total": gas_catalog.height,
            "future_ppr_objects_with_start": len(future_ppr),
            "future_ppr_objects_mapped_candidates": sum(
                r["source_object_key"] in ppr_keys for r in future_ppr),
            "customer_confirmed_links": sum(
                r["mapping_status"] == "customer_confirmed" for r in mapping["links"]),
        },
        "retrospective_audit": {
            "valid_as_new_schedule_metric": False,
            "reason": "schedule and candidate mapping were received after these predictions",
            "existing_policy_proxy": _outcomes(historical_rows, historical_positives),
            "context_status_counts": context_counts(historical_rows),
            "planned_overlap_alerts": len(retro_overlap),
            "planned_overlap_proxy_hits": sum(r["y"] == 1 for r in retro_overlap),
            "hypothetical_remaining_proxy_if_overlap_hidden_not_deployable": _outcomes(
                [r for r in historical_rows
                 if r["context_status"] != "schedule_overlap_unconfirmed"]),
            "planned_overlap_by_source": dict(sorted(Counter(
                m["source_object_key"] for r in retro_overlap
                for m in r["schedule_matches"]).items())),
        },
        "prospective": {
            "status": status,
            "labels_mature_through": str(labels_mature_through)
            if labels_mature_through else None,
            "issued_alerts_since_release": len(future_rows),
            "context_status_counts": context_counts(future_rows),
            "planned_overlap_alerts": len(future_overlap),
            "matured_policy_proxy": _outcomes(mature_rows, positives)
            if status == "descriptive_proxy_metrics_available" else None,
            "planned_cause_review_metrics": review_metrics,
            "planned_cause_review_note": (
                "human adjudication only; require evidence from actual work log"
                if review_metrics is not None else
                "no confirmed actual-work log or dispatcher decisions"),
            "product_gate_passed": False,
            "product_gate_reason": "no independent new holdouts, baseline comparison or confirmed work outcome",
        },
        "policy_effect_claim": "none: context does not change alerts or their ranking",
    }
