"""Слой контрактов: словари, схемы C2, зеркало C1. Эти тесты защищают стыки между ролями."""
import importlib.util
import json
import typing
from pathlib import Path

import pytest
from app import vocab
from app.main import create_app
from app.schemas import common
from app.schemas import ml as ml_mirror

ROOT = Path(__file__).resolve().parents[1]
ML_CONTRACT = ROOT / "ml" / "src" / "mkl" / "product_contract.py"


def _literal(tp) -> set[str]:
    return set(typing.get_args(tp))


@pytest.mark.parametrize(("vocab_name", "literal"), [
    ("scenario", common.Scenario),
    ("kind", common.Kind),
    ("score_type", common.ScoreType),
    ("source", common.Source),
    ("action", common.Action),
    ("reason_code", common.ReasonCode),
    ("outcome_manual", common.OutcomeManual),
    ("outcome_auto", common.OutcomeAuto),
    ("result_status", common.ResultStatus),
    ("work_order_status", common.WorkOrderStatus),
    ("work_order_priority", common.Priority),
    ("event_class", common.EventClass),
    ("roles", common.Role),
])
def test_vocabulary_matches_schema_literals(vocab_name, literal):
    assert set(vocab.codes(vocab_name)) == _literal(literal)


def test_permissions_reference_known_roles():
    roles = set(vocab.codes("roles"))
    for perm, holders in vocab.load()["permissions"].items():
        assert set(holders) <= roles, perm


def test_reason_codes_reference_known_actions():
    actions = set(vocab.codes("action"))
    for item in vocab.load()["reason_code"]:
        assert set(item["actions"]) <= actions, item["code"]


def test_work_order_transitions_are_closed():
    statuses = set(vocab.codes("work_order_status"))
    transitions = vocab.load()["work_order_transitions"]
    assert set(transitions) == statuses
    for targets in transitions.values():
        assert set(targets) <= statuses


def _load_ml_contract():
    spec = importlib.util.spec_from_file_location("ml_product_contract", ML_CONTRACT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ml_mirror_matches_ml_contract():
    """backend/app/schemas/ml.py — копия ml/src/mkl/product_contract.py; схемы обязаны совпадать."""
    original = _load_ml_contract()
    names = [n for n, obj in vars(original).items()
             if isinstance(obj, type) and hasattr(obj, "model_json_schema")
             and obj.__module__ == original.__name__]
    assert names, "в product_contract нет моделей"
    for name in names:
        mirror = getattr(ml_mirror, name)
        assert mirror.model_json_schema() == getattr(original, name).model_json_schema(), name


def test_openapi_builds_and_has_all_sections():
    paths = create_app().openapi()["paths"]
    for path in ["/api/v1/auth/login", "/api/v1/forecasts", "/api/v1/forecasts/{forecast_id}",
                 "/api/v1/work-orders", "/api/v1/events", "/api/v1/ingest/events",
                 "/api/v1/schema.geojson", "/api/v1/quality", "/api/v1/stream",
                 "/api/v1/system/status", "/api/v1/admin/run-daily"]:
        assert path in paths


def test_ml_fixtures_validate_against_mirror():
    """Фикстуры ML (пишет scripts/export_contracts.py) читаются зеркалом backend без ошибок."""
    fixtures = ROOT / "contracts" / "fixtures"
    checks = {"ml_score_2026-06-15.json": ml_mirror.ScoreResponse,
              "ml_guard_weekly_2026-06-15.json": ml_mirror.WeeklyResponse}
    present = [name for name in checks if (fixtures / name).is_file()]
    if not present:
        pytest.skip("фикстуры ML ещё не выгружены: python scripts/export_contracts.py")
    for name in present:
        checks[name].model_validate(json.loads((fixtures / name).read_text(encoding="utf-8")))
