"""Аудит семантики датчиков на синтетических событиях, без данных заказчика.

Суточная панель строится продакшн-функцией panel.build_daily_channel по тем же
событиям: аудит читает n_events и n_alarms из неё, и расхождение колонок со
схемой ingest всплыло бы здесь, а не на полном прогоне.
"""
import datetime as dt
import json
from pathlib import Path

from conftest import insert_event
from mkl import panel
from mkl import sensor_audit as audit

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "analysis" / "experiments" / "deep-data-audit"
PUMP = "Состояние насоса"


def _by(rows, key):
    return {r[key]: r for r in rows}


def _keys(node) -> set:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node))
    return set()


def test_unmapped_channel_and_numeric_fraction(ev_con):
    insert_event(ev_con, 1, 1, "2024-01-01 00:00:00", "Авария", alarm=True,
                 stype="Тип")
    insert_event(ev_con, 2, 1, "2024-01-01 00:00:00", "12.5", val_num=12.5,
                 stype="Тип")
    insert_event(ev_con, 3, 2, "2024-01-01 00:00:00", "Неизвестно", stype=None)
    panel.build_daily_channel(ev_con)

    rows = _by(audit.sensor_overview(ev_con), "sensor_type")
    assert rows["Тип"]["events"] == 2
    assert rows["Тип"]["alarms"] == 1
    assert rows["Тип"]["distinct_values"] == 2
    assert rows["Тип"]["numeric_event_fraction"] == 0.5
    assert rows[audit.UNMAPPED]["observed_channels"] == 1
    assert {r["sensor_type"] for r in audit.value_top(ev_con)} == {"Тип", audit.UNMAPPED}


def test_fault_counts_as_mixed_only_when_another_state_shares_the_second(ev_con):
    insert_event(ev_con, 1, 10, "2024-03-01 10:00:00", "Неисправен", stype=PUMP)
    insert_event(ev_con, 2, 10, "2024-03-01 10:00:00", "Обесточен", stype=PUMP)
    start, end = dt.date(2024, 1, 1), dt.date(2026, 6, 30)

    row = _by(audit.same_second_states(ev_con, start, end), "sensor_type")[PUMP]
    assert (row["fault_seconds"], row["mixed_fault_seconds"]) == (1, 1)
    assert row["maximum_states_in_one_second"] == 2

    insert_event(ev_con, 3, 10, "2024-03-02 10:00:00", "Неисправен", stype=PUMP)
    row = _by(audit.same_second_states(ev_con, start, end), "sensor_type")[PUMP]
    assert (row["fault_seconds"], row["mixed_fault_seconds"]) == (2, 1)
    assert row["observed_seconds"] == 2


def test_same_second_window_and_type_filter(ev_con):
    """Окно режется по суткам, типы вне списка PR #7 не считаются."""
    insert_event(ev_con, 1, 10, "2023-12-31 23:59:59", "Неисправен", stype=PUMP)
    insert_event(ev_con, 2, 11, "2024-03-01 10:00:00", "Обнаружено движение",
                 stype="Датчик движения")
    rows = audit.same_second_states(ev_con, dt.date(2024, 1, 1), dt.date(2026, 6, 30))
    assert rows == []


def test_target_start_requires_observed_non_target_previous_day(ev_con):
    """Порт test_target_audit из PR #7: дни 1–3 подряд, 4 без данных, 5 снова цель."""
    insert_event(ev_con, 1, 1, "2024-01-01 08:00:00", "Норма", stype=PUMP)
    for i, day in enumerate(("2024-01-02", "2024-01-03", "2024-01-05"), start=2):
        insert_event(ev_con, i, 1, f"{day} 08:00:00", "Неисправен", stype=PUMP)
    panel.build_daily_channel(ev_con)

    row = _by(audit.target_candidates(ev_con)["candidates"], "candidate")["pump_fault_signal"]
    assert row["observed_channel_days"] == 4
    assert row["target_channel_days"] == 3
    assert row["repeated_next_day"] == 1
    assert row["starts_after_observed_non_target_day"] == 1
    assert row["target_days_outside_panel"] == 0


def test_day_without_telemetry_is_not_a_clean_day(ev_con):
    """Норма 01.01, данных за 02.01 нет, цель 03.01: начала эпизода нет."""
    insert_event(ev_con, 1, 1, "2024-01-01 08:00:00", "Норма", stype=PUMP)
    insert_event(ev_con, 2, 1, "2024-01-03 08:00:00", "Неисправен", stype=PUMP)
    panel.build_daily_channel(ev_con)

    row = _by(audit.target_candidates(ev_con)["candidates"], "candidate")["pump_fault_signal"]
    assert row["target_channel_days"] == 1
    assert row["starts_after_observed_non_target_day"] == 0


def test_temperature_overflow_counts_only_in_raw_variant(ev_con):
    """-3276 — переполнение int16: в сырой цели оно выход за 3–40, в _valid нет."""
    temp = "Датчик температуры"
    insert_event(ev_con, 1, 5, "2024-02-01 00:00:00", "-3276", val_num=-3276.0, stype=temp)
    insert_event(ev_con, 2, 5, "2024-02-02 00:00:00", "45", val_num=45.0, stype=temp)
    insert_event(ev_con, 3, 5, "2024-02-03 00:00:00", "20", val_num=20.0, stype=temp)
    panel.build_daily_channel(ev_con)

    rows = _by(audit.target_candidates(ev_con)["candidates"], "candidate")
    assert rows["temperature_outside_3_40"]["target_channel_days"] == 2
    assert rows["temperature_outside_3_40_valid"]["target_channel_days"] == 1
    assert rows["temperature_outside_3_40"]["observed_channel_days"] == 3


def test_report_is_strict_json_and_matches_itself(ev_con):
    insert_event(ev_con, 1, 10, "2024-03-01 10:00:00", "Неисправен", stype=PUMP)
    insert_event(ev_con, 2, 10, "2024-03-01 10:00:00", "Норма", stype=PUMP)
    panel.build_daily_channel(ev_con)
    report = audit.audit_report(ev_con, second_start=dt.date(2024, 1, 1),
                                second_end=dt.date(2026, 6, 30))
    json.dumps(report, ensure_ascii=False, allow_nan=False, default=str)

    sensor_ref = {"sensor_types": report["sensor_types"],
                  "same_second_state_audit_2024_2026": report["same_second_state_audit"]["rows"]}
    target_ref = {"candidates": [r for r in report["targets"]["candidates"]
                                 if r["candidate"] != "temperature_outside_3_40_valid"]}
    parity = audit.compare_with_reference(report, sensor_ref, target_ref)
    assert parity["mismatches"] == []
    assert parity["checked_values"] > 0


def test_comparison_reports_delta_and_missing_rows():
    new = {"sensor_types": [{"sensor_type": "A", "events": 10}],
           "same_second_state_audit": {"rows": []},
           "targets": {"candidates": []}}
    ref = {"sensor_types": [{"sensor_type": "A", "events": 12.0},
                            {"sensor_type": "B", "events": 1}],
           "same_second_state_audit_2024_2026": []}
    parity = audit.compare_with_reference(new, ref, {"candidates": []})
    got = {(m["sensor_type"], m.get("field"), m.get("delta"), m.get("missing_in"))
           for m in parity["mismatches"]}
    assert ("A", "events", -2.0, None) in got
    assert ("B", None, None, "new") in got


def test_script_runs_end_to_end_on_synthetic_parquet(ev_con, tmp_path, monkeypatch):
    """Скрипт целиком: parquet в interim, справочник CSV, строгий JSON на выходе.

    На данных заказчика прогон стоит десятки минут, поэтому чтение файлов,
    запрос scope и запись отчёта проверяются здесь, на трёх событиях.
    """
    import sys

    from mkl import db
    from mkl.config import Paths
    from scripts import audit_sensor_semantics as runner

    insert_event(ev_con, 1, 10, "2024-03-01 10:00:00", "Неисправен", stype=PUMP)
    insert_event(ev_con, 2, 10, "2024-03-01 10:00:00", "Норма", stype=PUMP)
    insert_event(ev_con, 3, 11, "2024-03-02 10:00:00", "21", val_num=21.0,
                 stype="Датчик температуры")
    panel.build_daily_channel(ev_con)
    interim, materials = tmp_path / "interim", tmp_path / "materials"
    interim.mkdir()
    materials.mkdir()
    for table, name in (("ev", "events_year=2024"), ("daily_channel", "daily_channel")):
        ev_con.execute(f"COPY {table} TO '{(interim / name).as_posix()}.parquet' (FORMAT PARQUET)")
    (materials / runner.CATALOG).write_text(
        "ид_канала_данных,тип_датчика\n10,Состояние насоса\n11,Датчик температуры\n",
        encoding="utf-8")
    paths = Paths(interim=interim, materials=materials, tmp=tmp_path / "tmp")
    monkeypatch.setattr(db, "PATHS", paths)
    monkeypatch.setattr(runner, "PATHS", paths)
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["audit_sensor_semantics.py", "--output", str(out),
                                      "--threads", "1", "--memory-limit", "500MB",
                                      "--temp-dir", str(tmp_path / "spill")])
    runner.main()

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["scope"]["events"] == 3
    assert report["scope"]["catalog_channels"] == 2
    assert [f["file"] for f in report["inputs"]] == ["events_year=2024.parquet",
                                                     "daily_channel.parquet"]
    pump = _by(report["same_second_state_audit"]["rows"], "sensor_type")[PUMP]
    assert pump["mixed_fault_seconds"] == 1
    # Синтетика не похожа на журнал заказчика: сверка обязана это показать.
    assert report["parity_with_pr7"]["mismatches"]
    # Отчёт уходит в git: идентификаторов каналов в нём быть не должно.
    assert not _keys(report) & {"ch", "channel_id", "eid"}


def test_reference_reports_of_pr7_carry_the_compared_fields():
    """Сверка опирается на поля JSON из PR #7: переименование сломало бы её молча."""
    sensor = json.loads((REFERENCE / "sensor-audit.json").read_text(encoding="utf-8"))
    target = json.loads((REFERENCE / "target-audit.json").read_text(encoding="utf-8"))
    for section, rows in (("sensor_types", sensor["sensor_types"]),
                          ("same_second", sensor["same_second_state_audit_2024_2026"]),
                          ("candidates", target["candidates"])):
        key, fields = audit._PARITY[section]
        assert all(key in r and all(f in r for f in fields) for r in rows), section
    names = {r["candidate"] for r in target["candidates"]}
    assert names == set(audit.CANDIDATES) - {"temperature_outside_3_40_valid"}
