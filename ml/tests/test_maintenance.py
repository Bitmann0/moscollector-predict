import datetime as dt
import json

import pytest

from mkl.maintenance import (available_asof, load_mapping, maintenance_context,
                             parse_ppr, parse_to)
from scripts.annotate_maintenance_alerts import annotate


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
