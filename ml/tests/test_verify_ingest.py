"""Сверка приёма с аудитом: scripts/verify_ingest.py на синтетике.

Данных заказчика нет: профиль и аудит повторяют формат analysis/annual_profiles.json
и analysis/deep_audit.json, год 2099. Сквозной тест прогоняет настоящие
ingest.build_events и panel.build_daily_channel, чтобы сверка проверялась на
тех файлах, которые пишет пайплайн, а не на подогнанных под неё.
"""
import json

import duckdb
import pytest

from mkl import ingest, panel
from scripts import verify_ingest as vi

_HEADER = "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
# Пять строк после внешнего заголовка: один точный повтор и один заголовок внутри.
_ROWS = [
    "100,1,2099-01-01,10:00:00,f,Норма\n",
    "100,1,2099-01-01,10:00:00,f,Норма\n",
    _HEADER,
    "101,2,2099-01-01,11:00:00,t,Обнаружен дым\n",
    "102,1,2099-01-02,09:00:00,f,Норма\n",
]


def _profile(year=2099, rows=5, alarm_raw=None):
    alarm_raw = alarm_raw or {"f": 3, "t": 1, "тревожное": 1}
    return {"year": str(year), "summary": {"row_count": rows},
            "alarm_raw": [{"alarm_raw": v, "n": n} for v, n in alarm_raw.items()]}


def _audit(year=2099, excess=1):
    return {"duplicates": {str(year): {"exact_duplicate_excess": excess}}}


def _expected(**kw):
    return vi.audit_expectations([_profile(**kw)], _audit())


def _codes(report):
    return [e["code"] for e in report["errors"]]


def test_expectation_is_rows_minus_headers_minus_exact_duplicates():
    exp = _expected()[2099]
    assert exp["expected_events"] == 3
    assert exp["embedded_headers"] == 1
    assert exp["exact_duplicates"] == 1
    assert exp["unknown_alarm_values"] == []


@pytest.mark.parametrize("excess", [None, "absent"])
def test_missing_or_null_duplicate_count_means_zero(excess):
    """В deep_audit.json у 2025 exact_duplicate_excess — null, у 2020 года нет."""
    audit = {"duplicates": {}} if excess == "absent" else _audit(excess=None)
    exp = vi.audit_expectations([_profile(rows=4, alarm_raw={"f": 4})], audit)[2099]
    assert exp["exact_duplicates"] == 0
    assert exp["expected_events"] == 4


def test_matching_counts_reconcile():
    events = {2099: {"events": 3, "alarms": 1, "outside_year": 0}}
    daily = {2099: {"n_events": 3, "n_alarms": 1}}
    report = vi.reconcile(_expected(), events, daily)
    assert report["ok"], report["errors"]
    assert report["years"]["2099"]["missing_events"] == 0
    assert report["totals"]["events"] == report["totals"]["expected_events"] == 3


def test_lost_row_is_an_error():
    events = {2099: {"events": 2, "alarms": 1, "outside_year": 0}}
    daily = {2099: {"n_events": 2, "n_alarms": 1}}
    report = vi.reconcile(_expected(), events, daily)
    assert not report["ok"]
    assert report["years"]["2099"]["missing_events"] == 1
    assert _codes(report) == ["missing_events"]


def test_extra_rows_are_an_error_too():
    """Размноженное LEFT JOIN событие даёт отрицательную разницу, не ноль."""
    events = {2099: {"events": 4, "alarms": 1, "outside_year": 0}}
    report = vi.reconcile(_expected(), events, {2099: {"n_events": 4, "n_alarms": 1}})
    assert report["years"]["2099"]["missing_events"] == -1
    assert "missing_events" in _codes(report)


def test_unknown_alarm_value_is_an_error():
    """Приём превратил бы «1» в false молча: src/mkl/ingest.py:124."""
    exp = _expected(alarm_raw={"f": 2, "1": 1, "t": 1, "тревожное": 1})
    events = {2099: {"events": 3, "alarms": 1, "outside_year": 0}}
    report = vi.reconcile(exp, events, {2099: {"n_events": 3, "n_alarms": 1}})
    assert not report["ok"]
    err = next(e for e in report["errors"] if e["code"] == "unknown_alarm_values")
    assert err["detail"] == ["1"]


def test_alarm_values_are_case_insensitive_but_header_is_not_an_alarm():
    exp = _expected(alarm_raw={"F": 2, "True": 1, "тревожное": 1})[2099]
    assert exp["unknown_alarm_values"] == []
    assert exp["source_alarm_true"] == 1


def test_alarm_that_became_false_is_caught():
    """t в исходнике без повтора обязан остаться тревогой."""
    events = {2099: {"events": 3, "alarms": 0, "outside_year": 0}}
    audit = _audit(excess=0)
    exp = vi.audit_expectations([_profile(rows=4, alarm_raw={"f": 2, "t": 1,
                                                             "тревожное": 1})], audit)
    report = vi.reconcile(exp, events, {2099: {"n_events": 3, "n_alarms": 0}})
    assert _codes(report) == ["alarms_out_of_bounds"]


def test_absent_year_is_listed_not_failed_unless_required():
    expected = vi.audit_expectations(
        [_profile(year=2098), _profile(year=2099)],
        {"duplicates": {"2098": {"exact_duplicate_excess": 1},
                        "2099": {"exact_duplicate_excess": 1}}})
    events = {2099: {"events": 3, "alarms": 1, "outside_year": 0}}
    daily = {2099: {"n_events": 3, "n_alarms": 1}}
    report = vi.reconcile(expected, events, daily)
    assert report["ok"]
    assert report["not_present"] == [2098]
    strict = vi.reconcile(expected, events, daily, require_all=True)
    assert _codes(strict) == ["year_not_present"]


def test_panel_must_sum_to_events():
    events = {2099: {"events": 3, "alarms": 1, "outside_year": 0}}
    report = vi.reconcile(_expected(), events, {2099: {"n_events": 2, "n_alarms": 1}})
    assert _codes(report) == ["daily_events_mismatch"]
    report = vi.reconcile(_expected(), events, {2099: {"n_events": 3, "n_alarms": 0}})
    assert _codes(report) == ["daily_alarms_mismatch"]
    assert "daily_channel_absent" in _codes(vi.reconcile(_expected(), events, None))


def test_rows_dated_outside_the_file_year_are_flagged():
    events = {2099: {"events": 3, "alarms": 1, "outside_year": 1}}
    report = vi.reconcile(_expected(), events, {2099: {"n_events": 3, "n_alarms": 1}})
    assert _codes(report) == ["rows_outside_year"]


def test_end_to_end_on_real_ingest_and_panel(tmp_path, monkeypatch):
    """CSV → ingest.build_events → panel.build_daily_channel → main() == 0."""
    import polars as pl
    from mkl.config import Paths

    raw, interim = tmp_path / "raw", tmp_path / "interim"
    raw.mkdir(), interim.mkdir()
    monkeypatch.setattr(ingest, "PATHS", Paths(raw=raw, interim=interim))
    pl.DataFrame({
        "ch": [1, 2], "sys": ["s", "s"], "stype": ["Датчик дыма"] * 2,
        "tag": ["t", "t"], "sname": ["n", "n"], "obj": ["A", "A"],
        "obj_parent": ["P", "P"], "obj_kind": ["guardObject"] * 2,
        "obj_level": [3, 3], "tag_prefix": ["t", "t"], "picket": [1.0, 2.0],
    }).write_parquet(interim / "channels.parquet")
    (raw / "ext-journal-2099.csv").write_text(_HEADER + "".join(_ROWS), encoding="utf-8")
    assert ingest.build_events([2099]) == {2099: 3}

    con = duckdb.connect(":memory:")
    con.execute("CREATE VIEW ev AS SELECT * FROM read_parquet("
                f"'{(interim / 'events_year=2099.parquet').as_posix()}')")
    panel.build_daily_channel(con)
    con.execute(f"COPY daily_channel TO '{(interim / 'daily_channel.parquet').as_posix()}' "
                "(FORMAT PARQUET)")
    con.close()

    profiles, audit = tmp_path / "annual_profiles.json", tmp_path / "deep_audit.json"
    profiles.write_text(json.dumps([_profile()], ensure_ascii=False), encoding="utf-8")
    audit.write_text(json.dumps(_audit()), encoding="utf-8")
    out = tmp_path / "reports" / "ingest_reconciliation.json"
    args = ["--interim", str(interim), "--profiles", str(profiles), "--audit", str(audit),
            "--output", str(out), "--require-all", "--threads", "1"]
    assert vi.main(args) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["ok"]
    assert report["years"]["2099"]["events"] == 3
    assert report["years"]["2099"]["alarms"] == 1
    assert report["daily_channel"] == {"rows": 3, "channels": 2, "n_events": 3, "n_alarms": 1}
    assert report["inputs"]["events"] == ["events_year=2099.parquet"]

    # Тот же прогон против аудита, где строк на одну больше, — потеря.
    profiles.write_text(json.dumps([_profile(rows=6, alarm_raw={
        "f": 4, "t": 1, "тревожное": 1})], ensure_ascii=False), encoding="utf-8")
    assert vi.main(args) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["errors"][0]["code"] == "missing_events"


def test_missing_audit_file_is_exit_code_2(tmp_path):
    args = ["--interim", str(tmp_path), "--profiles", str(tmp_path / "no.json"),
            "--audit", str(tmp_path / "no.json"), "--output", str(tmp_path / "out.json")]
    assert vi.main(args) == 2
    assert not (tmp_path / "out.json").exists()
