import json

from mkl import experiments


def test_log_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "log.jsonl")
    experiments.log({"head": "A", "step": "B1", "pr_auc": 0.1})
    experiments.log({"head": "A", "step": "B3", "pr_auc": 0.4})
    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[1])["step"] == "B3"


def test_log_stamps_time_and_git_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "log.jsonl")
    experiments.log({"head": "A", "step": "B1"})
    rec = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8").strip())
    assert "ts" in rec and "git_sha" in rec


def test_best_returns_highest_metric(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "log.jsonl")
    experiments.log({"head": "A", "step": "B1", "pr_auc": 0.1})
    experiments.log({"head": "A", "step": "B3", "pr_auc": 0.4})
    experiments.log({"head": "B", "step": "B3", "pr_auc": 0.9})
    assert experiments.best("A", "pr_auc")["step"] == "B3"


def test_best_is_empty_without_log(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "missing.jsonl")
    assert experiments.best("A") == {}
