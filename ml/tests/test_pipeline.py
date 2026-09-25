"""Граф стадий: что от чего зависит и что устарело.

Оркестратора нет намеренно: Airflow и подобное решают задачу распределённого
расписания, которой у нас не стоит, а цена — инфраструктура, которую
принимающей команде придётся поднимать и понимать.
"""
import time

import pytest

from mkl import pipeline


def _stage(tmp_path, name, inputs, outputs, needs=()):
    return pipeline.Stage(name=name, title=name, command=["true"],
                          inputs=[tmp_path / i for i in inputs],
                          outputs=[tmp_path / o for o in outputs], needs=needs)


def _touch(p, when=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    if when is not None:
        import os
        os.utime(p, (when, when))


def test_stage_without_output_has_no_result(tmp_path):
    s = _stage(tmp_path, "a", ["in.txt"], ["out.txt"])
    _touch(tmp_path / "in.txt")
    assert s.state() == "нет результата"


def test_output_newer_than_input_is_fresh(tmp_path):
    s = _stage(tmp_path, "a", ["in.txt"], ["out.txt"])
    _touch(tmp_path / "in.txt", when=1000)
    _touch(tmp_path / "out.txt", when=2000)
    assert s.state() == "свежая"


def test_input_newer_than_output_makes_the_stage_stale(tmp_path):
    s = _stage(tmp_path, "a", ["in.txt"], ["out.txt"])
    _touch(tmp_path / "out.txt", when=1000)
    _touch(tmp_path / "in.txt", when=2000)
    assert s.state() == "устарела"


def test_the_oldest_output_decides(tmp_path):
    """Одна недосчитанная выходная таблица делает устаревшей всю стадию."""
    s = _stage(tmp_path, "a", ["in.txt"], ["one.txt", "two.txt"])
    _touch(tmp_path / "in.txt", when=1500)
    _touch(tmp_path / "one.txt", when=2000)
    _touch(tmp_path / "two.txt", when=1000)
    assert s.state() == "устарела"


def test_plan_pulls_in_dependents_of_a_rebuilt_stage(monkeypatch, tmp_path):
    """Пересборка входа обесценивает выход, даже если сам выход выглядит свежим."""
    a = _stage(tmp_path, "a", ["in.txt"], ["a.txt"])
    b = _stage(tmp_path, "b", ["a.txt"], ["b.txt"], needs=("a",))
    _touch(tmp_path / "in.txt", when=3000)
    _touch(tmp_path / "a.txt", when=1000)   # a устарела
    _touch(tmp_path / "b.txt", when=9000)   # b выглядит свежей
    monkeypatch.setattr(pipeline, "stages", lambda: [a, b])
    assert [s.name for s in pipeline.plan()] == ["a", "b"]


def test_plan_stops_at_the_requested_stage(monkeypatch, tmp_path):
    a = _stage(tmp_path, "a", ["in.txt"], ["a.txt"])
    b = _stage(tmp_path, "b", ["a.txt"], ["b.txt"], needs=("a",))
    _touch(tmp_path / "in.txt", when=3000)
    monkeypatch.setattr(pipeline, "stages", lambda: [a, b])
    assert [s.name for s in pipeline.plan("a")] == ["a"]


def test_force_rebuilds_even_fresh_stages(monkeypatch, tmp_path):
    a = _stage(tmp_path, "a", ["in.txt"], ["a.txt"])
    _touch(tmp_path / "in.txt", when=1000)
    _touch(tmp_path / "a.txt", when=2000)
    monkeypatch.setattr(pipeline, "stages", lambda: [a])
    assert pipeline.plan() == []
    assert [s.name for s in pipeline.plan(force=True)] == ["a"]


def test_real_graph_declares_dependencies_that_exist():
    names = {s.name for s in pipeline.stages()}
    for s in pipeline.stages():
        assert set(s.needs) <= names, f"{s.name} зависит от несуществующей стадии"


def test_ingest_depends_on_its_own_code():
    """Правка дедупликации меняет данные. Без этой связи пересборку пришлось бы
    помнить руками."""
    ing = pipeline.by_name()["ingest"]
    assert any(p.name == "ingest.py" for p in ing.inputs)
