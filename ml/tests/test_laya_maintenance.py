import datetime as dt

import pytest

from scripts.eval_laya_maintenance import (MODEL_SHA, QUESTION_HASH,
                                           render_scenarios, summarize)


def test_paired_scenarios_change_only_plan_context():
    context = {"status": "schedule_overlap_unconfirmed", "matches": [{
        "kind": "planned_to_month", "mapping_status": "inferred",
        "month": 11, "work_type": "ТО"}]}
    base, planned = render_scenarios("Same sensor history.", context,
                                     dt.date(2026, 11, 2))
    assert base.startswith("Same sensor history.")
    assert planned.startswith("Same sensor history.")
    assert "no schedule entry" in base
    assert "planned sometime in month 11" in planned
    assert "actual execution are unknown" in planned
    assert "No cause is confirmed" in planned


def test_paired_scenarios_fail_closed_when_plan_unavailable():
    with pytest.raises(ValueError, match="available planned-work"):
        render_scenarios("state", {"status": "unavailable_asof", "matches": []},
                         dt.date(2026, 4, 24))


def test_laya_summary_is_behavior_only_and_requires_complete_pairs():
    cases = [{"pair_id": "one", "variant": v} for v in ("no_plan", "planned_month")]
    scores = [{"pair_id": "one", "variant": v,
               "answers": {"route": {"choice": route},
                           "planned_cause_confirmed": {"noul": 0.2}},
               "model_revision": MODEL_SHA, "question_hash": QUESTION_HASH}
              for v, route in (("no_plan", "remote_link_check"),
                               ("planned_month", "verify_planned_work"))]
    result = summarize(cases, scores)
    assert result["paired_route_changes"] == 1
    assert result["predictive_metric_valid"] is False
    with pytest.raises(ValueError, match="missing or duplicate"):
        summarize(cases, scores[:1])
