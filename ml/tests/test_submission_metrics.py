"""Реестр метрик для сдачи совпадает с отчётами, из которых собран.

Отчёт пересчитали, а реестр забыли — числа в README, документации и слайдах
разошлись бы с источником (риск R6 плана команды). Тест ловит ровно это.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_submission_metrics.py"


def _builder():
    spec = importlib.util.spec_from_file_location("build_submission_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_matches_reports():
    builder = _builder()
    committed = builder.OUT.read_text(encoding="utf-8")
    assert committed == builder.render(builder.build()), (
        "reports/SUBMISSION_METRICS.json устарел: python scripts/build_submission_metrics.py")


def _blocks(data: dict):
    for scenario in data["scenarios"]:
        for key in ("evaluations", "comparison", "base_rate"):
            yield from scenario[key]
    yield from data["runtime"]
    yield data["holdout"]
    for setup in data["rejected_setups"]:
        yield from setup["results"]
    yield from data["rejected_levers"]


def test_every_number_block_names_source_and_command():
    data = json.loads(_builder().OUT.read_text(encoding="utf-8"))
    for block in _blocks(data):
        assert block.get("source"), block
        assert "command" in block, block


def test_quality_screen_covers_product_scenarios():
    data = json.loads(_builder().OUT.read_text(encoding="utf-8"))
    codes = [s["code"] for s in data["scenarios"]]
    assert list(data["quality_screen"]) == codes
    for code in codes:
        ref = data["quality_screen"][code]
        assert set(ref) == {"base_rate", "rule_precision", "period", "source", "note"}
        assert 0 < ref["base_rate"] < 1
