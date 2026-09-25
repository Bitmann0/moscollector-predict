import datetime as dt
import json

import pytest
import polars as pl

from mkl.maintenance import (available_asof, load_mapping, maintenance_context,
                             parse_ppr, parse_to, snapshot_paths)
from scripts.annotate_maintenance_alerts import annotate
from mkl.maintenance_eval import evaluate_link_schedule


def test_ppr_preserves_missing_dates_and_never_assigns_catalog_object():
    rows = [
        (10, ("РЭК", "Январь", "Объект 1", 56, dt.datetime(2026, 1, 12),
              "до 900 13.01.2026", dt.datetime(2026, 1, 22), dt.datetime(2026, 1, 27))),
        (11, (None, None, "Объект 2", 13, None, None, None, None)),
    ]
    result = parse_ppr(rows)
    assert [r["source_object_key"] for r in result] == ["ppr:1", "ppr:2"]
    assert result[0]["planned_dismantle"] == "2026-01-12"
    assert result[1]["quality_flags"] == ["no_planned_dates"]
    assert all(r["internal_obj"] is None for r in result)


def test_to_keeps_hidden_equipment_and_month_precision():
    rows = [
        (6, (None, 1, "Объект", None, None, None), False),
        (7, (None, None, None, "Газоанализаторы", 56, "шт.", None, "ТО", None, "ТО+ТР"), True),
        (8, (None, None, None, "Марка", "Кол-во", "ед."), True),
    ]
    equipment, work = parse_to(rows)
    assert len(equipment) == 1
    assert equipment[0]["hidden_source_row"] is True
    assert equipment[0]["source_object_key"] == "to:1"
    assert equipment[0]["source_name"] == "Объект"
    assert [(r["month"], r["work_type"], r["date_precision"]) for r in work] == [
        (2, "ТО", "month"), (4, "ТО+ТР", "month")]
    assert all(r["internal_obj"] is None for r in work)


def test_unknown_work_marker_fails_closed():
    with pytest.raises(ValueError, match="unknown marker"):
        parse_to([(6, (None, 1, "Объект"), False),
                  (7, (None, None, None, "БП", 1, "шт.", "готово"), False)])


def test_schedule_is_unavailable_to_earlier_backtest():
    received = dt.date(2026, 9, 25)
    assert not available_asof(received, dt.date(2026, 4, 1))
    assert available_asof(received, received)


def test_next_year_snapshot_can_be_selected_without_code_change(monkeypatch, tmp_path):
    monkeypatch.setenv("MKL_MAINTENANCE_SCHEDULE", "data/interim/maintenance_2027.json")
    monkeypatch.setenv("MKL_MAINTENANCE_MAPPING", "resources/mapping_2027.json")
    schedule, mapping = snapshot_paths(tmp_path, tmp_path / "data" / "interim")
    assert schedule == tmp_path / "data" / "interim" / "maintenance_2027.json"
    assert mapping == tmp_path / "resources" / "mapping_2027.json"


@pytest.fixture
def context_inputs():
    schedule = {
        "available_from": "2026-09-25",
        "source_metadata": {"ppr": {"sha256": "abc"}, "to": {"sha256": "def"}},
        "ppr_objects": [{"source_object_key": "ppr:9",
                         "planned_dismantle": "2026-10-15",
                         "planned_commission": "2026-10-30"}],
        "to_equipment": [{"source_object_key": "to:9", "source_row": 77,
                          "equipment_type": "ГАСБ"},
                         {"source_object_key": "to:9", "source_row": 78,
                          "equipment_type": "ИБП"}],
        "to_work_months": [{"source_object_key": "to:9", "equipment_source_row": 77,
                            "month": 11, "work_type": "ТО"},
                           {"source_object_key": "to:9", "equipment_source_row": 78,
                            "month": 12, "work_type": "ТО"}],
    }
    mapping = {"schema_version": 1, "available_from": "2026-09-25",
               "source_sha256": {"ppr": "abc", "to": "def"},
               "links": [{"source_object_key": key, "obj_parent": "3828",
                          "mapping_status": "inferred", "equipment_scope": "Газовый датчик"}
                         for key in ("ppr:9", "to:9")]}
    return schedule, mapping


def _context(schedule, mapping, *, asof="2026-09-25", start="2026-11-03",
             end="2026-11-04", obj="3828", stype="Газовый датчик"):
    return maintenance_context(schedule, mapping, obj_parent=obj, sensor_type=stype,
                               asof=dt.date.fromisoformat(asof),
                               window_start=dt.date.fromisoformat(start),
                               window_end=dt.date.fromisoformat(end))


def test_asof_guard_and_unknown_mapping(context_inputs):
    schedule, mapping = context_inputs
    old = _context(schedule, mapping, asof="2026-09-24")
    assert old["status"] == "unavailable_asof"
    assert old["matches"] == []
    assert _context(schedule, mapping, obj="999")["status"] == "unmapped"
    assert _context(schedule, mapping, stype="Состояние насоса")["status"] == "outside_equipment_scope"


def test_to_month_context_is_uncertain_and_equipment_scoped(context_inputs):
    schedule, mapping = context_inputs
    result = _context(schedule, mapping)
    assert result["status"] == "schedule_overlap_unconfirmed"
    assert result["matches"] == [{
        "source_object_key": "to:9", "obj_parent": "3828", "mapping_status": "inferred",
        "source": "to", "kind": "planned_to_month", "date_precision": "month",
        "month": 11, "work_type": "ТО", "equipment_source_row": 77}]
    # The December UPS row is not a gas-sensor work marker.
    assert _context(schedule, mapping, start="2026-12-03", end="2026-12-04")["matches"] == []
    assert _context(schedule, mapping, start="2027-01-02", end="2027-01-03")["status"] == "outside_schedule_year"


def test_ppr_context_and_half_open_alert_window(context_inputs):
    schedule, mapping = context_inputs
    ppr = _context(schedule, mapping, start="2026-10-20", end="2026-10-21")
    assert ppr["matches"][0]["kind"] == "planned_ppr_window"
    assert ppr["matches"][0]["planned_end"] == "2026-10-30"
    alert = {"alert_id": "test-1", "asof": "2026-09-25",
             "valid_from": "2026-10-31T00:00:00",
             "valid_to": "2026-11-01T00:00:00",
             "address": {"obj_parent": "3828", "sensor_type": "Газовый датчик"},
             "risk": 0.8, "rank": 1}
    sidecar = annotate(alert, schedule, mapping)
    assert sidecar == {"alert_id": "test-1", "maintenance_context": {
        "status": "no_planned_overlap", "matches": []}}
    assert alert["risk"] == 0.8 and alert["rank"] == 1


def test_mapping_rejects_changed_source_or_unknown_key(context_inputs, tmp_path):
    schedule, mapping = context_inputs
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    assert load_mapping(path, schedule)["links"] == mapping["links"]
    mapping["source_sha256"]["to"] = "other"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(ValueError, match="hash changed"):
        load_mapping(path, schedule)
    mapping["source_sha256"]["to"] = "def"
    mapping["links"][0]["source_object_key"] = "ppr:999"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown source key"):
        load_mapping(path, schedule)


def test_link_monitor_separates_historical_audit_and_new_metrics(context_inputs):
    schedule, mapping = context_inputs
    scored = pl.DataFrame({
        "ch": [1, 1, 2],
        "day": [dt.date(2026, 9, 24), dt.date(2026, 11, 1), dt.date(2026, 11, 1)],
        "risk": [0.9, 0.9, 0.8], "y": [1, 1, 0],
    })
    channels = pl.DataFrame({"ch": [1, 2], "obj_parent": ["3828", "999"],
                             "stype": ["Газовый датчик", "Газовый датчик"]})
    report = evaluate_link_schedule(scored, channels, schedule, mapping,
                                    threshold=0.5,
                                    labels_mature_through=dt.date(2026, 11, 2))
    assert report["retrospective_audit"]["valid_as_new_schedule_metric"] is False
    assert report["retrospective_audit"]["existing_policy_proxy"]["alerts"] == 1
    assert report["mapping_coverage"]["catalog_gas_channels_mapped_candidates"] == 1
    assert report["mapping_coverage"]["customer_confirmed_links"] == 0
    prospective = report["prospective"]
    assert prospective["status"] == "descriptive_proxy_metrics_available"
    assert prospective["context_status_counts"] == {
        "schedule_overlap_unconfirmed": 1, "unmapped": 1}
    assert prospective["matured_policy_proxy"]["precision_lower_bound"] == 0.5
    assert prospective["matured_policy_proxy"]["recall_known"] == 1.0
    assert prospective["planned_cause_review_metrics"] is None
    assert prospective["product_gate_passed"] is False
    reviews = pl.DataFrame({
        "ch": [1, 2], "day": [dt.date(2026, 11, 1)] * 2,
        "verdict": ["confirmed_planned_cause", "confirmed_unrelated"],
    })
    reviewed = evaluate_link_schedule(
        scored, channels, schedule, mapping, threshold=0.5,
        labels_mature_through=dt.date(2026, 11, 2), reviews=reviews)
    review_metrics = reviewed["prospective"]["planned_cause_review_metrics"]
    assert review_metrics["flag_precision_known_only"] == 1.0
    assert review_metrics["flag_recall_among_reviewed_issued"] == 1.0


def test_link_monitor_does_not_report_unmatured_precision(context_inputs):
    schedule, mapping = context_inputs
    scored = pl.DataFrame({"ch": [1], "day": [dt.date(2026, 11, 1)],
                           "risk": [0.9], "y": [None]}).with_columns(pl.col("y").cast(pl.Int32))
    channels = pl.DataFrame({"ch": [1], "obj_parent": ["3828"],
                             "stype": ["Газовый датчик"]})
    pending = evaluate_link_schedule(scored, channels, schedule, mapping, threshold=0.5)
    assert pending["prospective"]["status"] == "label_maturity_not_declared"
    assert pending["prospective"]["matured_policy_proxy"] is None
    still_unknown = evaluate_link_schedule(
        scored, channels, schedule, mapping, threshold=0.5,
        labels_mature_through=dt.date(2026, 11, 2))
    assert still_unknown["prospective"]["status"] == "awaiting_observed_outcomes"


def test_link_monitor_rejects_duplicate_or_invalid_reviews(context_inputs):
    schedule, mapping = context_inputs
    scored = pl.DataFrame({"ch": [1], "day": [dt.date(2026, 11, 1)],
                           "risk": [0.9], "y": [1]})
    channels = pl.DataFrame({"ch": [1], "obj_parent": ["3828"],
                             "stype": ["Газовый датчик"]})
    duplicate = pl.DataFrame({"ch": [1, 1], "day": [dt.date(2026, 11, 1)] * 2,
                              "verdict": ["unknown", "confirmed_planned_cause"]})
    with pytest.raises(ValueError, match="duplicate review"):
        evaluate_link_schedule(scored, channels, schedule, mapping,
                               threshold=0.5, reviews=duplicate)
    invalid = duplicate.head(1).with_columns(pl.lit("maybe").alias("verdict"))
    with pytest.raises(ValueError, match="invalid maintenance review"):
        evaluate_link_schedule(scored, channels, schedule, mapping,
                               threshold=0.5, reviews=invalid)
