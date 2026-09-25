import datetime as dt

import pytest

from mkl.maintenance import available_asof, parse_ppr, parse_to


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
