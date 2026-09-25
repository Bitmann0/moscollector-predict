"""Словари C3 из contracts/vocabularies.json. Живое.

Тот же файл читают тесты и фронт (через /reason-codes и /me). Перечисления в
schemas/common.py обязаны совпадать с кодами здесь: это проверяет
tests/test_contract_layer.py.
"""
import json
from functools import lru_cache

from .config import get_settings


@lru_cache
def load() -> dict:
    path = get_settings().contracts_dir / "vocabularies.json"
    return json.loads(path.read_text(encoding="utf-8"))


def codes(name: str) -> list[str]:
    return [item["code"] for item in load()[name]]


def title(name: str, code: str) -> str:
    for item in load()[name]:
        if item["code"] == code:
            return item["title"]
    return code


def scenario(code: str) -> dict:
    return next(s for s in load()["scenario"] if s["code"] == code)


def scenario_by_head(head: str) -> dict | None:
    return next((s for s in load()["scenario"] if s["head"] == head), None)


def permissions_of(role: str) -> frozenset[str]:
    return frozenset(p for p, roles in load()["permissions"].items() if role in roles)


def priority_from_ml(ml_priority: str) -> str:
    for item in load()["work_order_priority"]:
        if item["ml_code"] == ml_priority:
            return item["code"]
    return "watch"
