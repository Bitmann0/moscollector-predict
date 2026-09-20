"""Скрипты должны хотя бы разбираться, а импортируемые — импортироваться.

Точки входа тестами не покрыты, и синтаксическая ошибка в них не проявляется до
следующего запуска, а запуск стоит десятки минут. Этот тест ловит ровно такой
класс поломок: правку внесли после прогона и узнали бы о ней через полчаса.

Импортируются только скрипты с защитой `if __name__ == "__main__"`. У пяти
скриптов её нет, и работа у них идёт на модульном уровне — импорт такого
скрипта запустил бы пересборку панели. Сам этот факт стоит держать на виду:
тест ниже фиксирует их поимённо, чтобы список не рос молча.
"""
import ast
import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.py"))


def _has_main_guard(path: Path) -> bool:
    return '__main__' in path.read_text(encoding="utf-8")


IMPORTABLE = [p for p in SCRIPTS if _has_main_guard(p)]

# Работа на модульном уровне: импортировать нельзя, запустится пересборка.
RUNS_ON_IMPORT = {"audit_labels", "build_features", "build_panel",
                  "build_states", "fetch_weather"}


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_script_parses(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("path", IMPORTABLE, ids=lambda p: p.stem)
def test_script_imports(path, monkeypatch):
    """Импорт исполняет модульный уровень: туда попадают чтение конфигурации,
    вычисление окон и разбор heads.yaml, и они тоже могут сломаться."""
    monkeypatch.syspath_prepend(str(path.parent))
    sys.modules.pop(path.stem, None)
    importlib.import_module(path.stem)


def test_list_of_scripts_running_on_import_does_not_grow():
    """Скрипт без защиты `__main__` нельзя ни импортировать, ни переиспользовать
    как модуль. Пять существующих терпимы, новые заводить не стоит."""
    got = {p.stem for p in SCRIPTS if not _has_main_guard(p)}
    assert got == RUNS_ON_IMPORT, f"изменился состав: {got ^ RUNS_ON_IMPORT}"
