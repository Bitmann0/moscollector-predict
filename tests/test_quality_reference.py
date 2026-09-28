"""Экран «Качество прогноза»: базовая частота и правило берутся из реестра метрик ML.

contracts/quality_reference.json — копия блока quality_screen реестра
ml/reports/SUBMISSION_METRICS.json. Разойтись им нельзя: числа на экране и в
документах сдачи обязаны совпадать (решение D12 плана команды).
"""
import json
from pathlib import Path

import pytest
from app import vocab

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "contracts" / "quality_reference.json"
REGISTRY = ROOT / "ml" / "reports" / "SUBMISSION_METRICS.json"
API = "/api/v1"


def _reference() -> dict:
    return json.loads(REFERENCE.read_text(encoding="utf-8"))["scenario"]


def test_reference_is_copy_of_ml_registry():
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    assert _reference() == registry["quality_screen"]


def test_reference_covers_every_scenario():
    assert set(_reference()) == set(vocab.codes("scenario"))


@pytest.mark.parametrize("scenario", ["sensor_link", "equipment_diag", "guard_weekly",
                                      "fire_risk", "flood_risk"])
def test_quality_returns_reference_numbers(admin, scenario):
    expected = _reference()[scenario]
    result = admin.get(f"{API}/quality", params={"scenario": scenario}).json()
    assert result["base_rate"] == expected["base_rate"]
    assert result["rule_precision"] == expected["rule_precision"]
    assert result["reference_period"] == expected["period"]
    assert result["reference_source"] == expected["source"]
    assert result["reference_note"] == expected["note"]
    assert result["base_rate"] is not None


def test_quality_without_reference_is_not_an_error(admin, monkeypatch):
    """Сценарий без записи в quality_reference.json: экран работает, опорных чисел нет."""
    from app.services import quality

    reference = {k: v for k, v in _reference().items() if k != "fire_risk"}
    monkeypatch.setattr(quality, "reference", lambda: reference)
    resp = admin.get(f"{API}/quality", params={"scenario": "fire_risk"})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["scenario"] == "fire_risk" and len(result["weeks"]) == 4
    for key in ("base_rate", "rule_precision", "reference_period", "reference_source",
                "reference_note"):
        assert result[key] is None, key


def test_quality_reference_source_files_exist():
    for item in _reference().values():
        assert (ROOT / item["source"]).is_file(), item["source"]
