import numpy as np
import pytest

from mkl import metrics


def test_precision_at_k_takes_top_scores():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.9, 0.8])
    assert metrics.precision_at_k(y, p, k=2) == pytest.approx(1.0)


def test_recall_at_k():
    y = np.array([1, 1, 1, 0])
    p = np.array([0.9, 0.8, 0.1, 0.2])
    assert metrics.recall_at_k(y, p, k=2) == pytest.approx(2 / 3)


def test_pr_auc_is_one_for_perfect_ranking():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.8, 0.9])
    assert metrics.pr_auc(y, p) == pytest.approx(1.0)


def test_pr_auc_is_nan_without_positives():
    assert np.isnan(metrics.pr_auc(np.zeros(5), np.random.rand(5)))


def test_threshold_for_budget_selects_that_many_alerts():
    p = np.array([0.1, 0.5, 0.9, 0.7, 0.3])
    thr = metrics.threshold_for_budget(p, budget=2)
    assert (p >= thr).sum() == 2


def test_budget_larger_than_data_is_clamped():
    p = np.array([0.1, 0.5])
    assert (p >= metrics.threshold_for_budget(p, budget=99)).sum() == 2


def test_lift_is_one_for_random_ranking():
    rng = np.random.default_rng(0)
    y = (rng.random(10000) < 0.2).astype(int)
    p = rng.random(10000)
    assert metrics.lift(y, p, k=1000) == pytest.approx(1.0, abs=0.25)


def test_pr_auc_norm_is_zero_for_random_ranking():
    rng = np.random.default_rng(21)
    for br in (0.01, 0.2, 0.6):
        y = (rng.random(20000) < br).astype(int)
        p = rng.random(20000)
        assert metrics.pr_auc_norm(y, p) == pytest.approx(0.0, abs=0.03)


def test_pr_auc_norm_is_one_for_perfect_ranking():
    y = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0])
    p = np.array([0.9, 0.8, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    assert metrics.pr_auc_norm(y, p) == pytest.approx(1.0)


def test_pr_auc_norm_does_not_reward_inflating_base_rate():
    """Сырой PR-AUC 0.96 при базовой ставке 0.62 хуже, чем 0.28 при 0.03."""
    rng = np.random.default_rng(22)
    y_common = (rng.random(20000) < 0.62).astype(int)
    p_common = np.clip(y_common * 0.5 + rng.normal(0, 0.45, 20000), 0, 1)
    y_rare = (rng.random(20000) < 0.03).astype(int)
    p_rare = np.clip(y_rare * 0.8 + rng.normal(0, 0.30, 20000), 0, 1)
    assert metrics.pr_auc(y_common, p_common) > metrics.pr_auc(y_rare, p_rare)
    assert metrics.pr_auc_norm(y_rare, p_rare) > metrics.pr_auc_norm(y_common, p_common)


def test_precision_at_recall_is_one_for_perfect_ranking():
    y = np.array([1, 1, 1, 0, 0, 0, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.1, 0.1, 0.1, 0.1, 0.1])
    out = metrics.precision_at_recall(y, p, target_recall=0.5)
    assert out["precision"] == pytest.approx(1.0)
    assert out["recall"] >= 0.5


def test_precision_at_recall_matches_base_rate_for_random_ranking():
    rng = np.random.default_rng(11)
    y = (rng.random(20000) < 0.1).astype(int)
    p = rng.random(20000)
    out = metrics.precision_at_recall(y, p, target_recall=0.5)
    assert out["precision"] == pytest.approx(0.1, abs=0.02)


def test_precision_at_recall_exposes_tiny_recall_behind_high_precision():
    """Высокая точность может достигаться на единичном верхнем алерте.
    Точность при Recall 0.5 показывает настоящую цену половины событий."""
    y = np.concatenate([[1], np.zeros(999, dtype=int), np.ones(99, dtype=int)])
    p = np.concatenate([[1.0], np.linspace(0.9, 0.5, 999), np.full(99, 0.4)])
    op = metrics.target_operating_point(y, p)
    assert op["feasible"] and op["precision"] >= 0.7
    assert op["recall"] < 0.05, "точность 0.7 держится лишь на одном алерте"
    assert metrics.precision_at_recall(y, p, 0.5)["precision"] < 0.2


def test_target_operating_point_found_for_perfect_ranking():
    y = np.array([1, 1, 1, 0, 0, 0, 0, 0, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    out = metrics.target_operating_point(y, p, min_precision=0.7)
    assert out["feasible"]
    assert out["precision"] >= 0.7
    assert out["recall"] == pytest.approx(1.0)


def test_target_operating_point_infeasible_for_random_ranking():
    rng = np.random.default_rng(7)
    y = (rng.random(5000) < 0.01).astype(int)
    p = rng.random(5000)
    out = metrics.target_operating_point(y, p, min_precision=0.7)
    assert not out["feasible"]
    assert "max_precision" in out


def test_target_operating_point_maximises_recall_under_constraint():
    y = np.array([1, 1, 0, 1, 0, 0, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2])
    out = metrics.target_operating_point(y, p, min_precision=0.7)
    assert out["feasible"]
    assert out["k"] == 4, "берём самую глубокую точку, где точность ещё держится"
    assert out["recall"] == pytest.approx(1.0)


def test_target_operating_point_handles_no_positives():
    assert not metrics.target_operating_point(np.zeros(10), np.random.rand(10))["feasible"]


def test_budget_curve_precision_falls_as_budget_grows():
    rng = np.random.default_rng(3)
    y = (rng.random(5000) < 0.05).astype(int)
    p = np.clip(y * 0.6 + rng.normal(0, 0.2, 5000), 0, 1)
    curve = metrics.budget_curve(y, p, (10, 100, 1000))
    assert [c["budget"] for c in curve] == [10, 100, 1000]
    assert curve[0]["precision"] >= curve[-1]["precision"]
    assert curve[0]["recall"] <= curve[-1]["recall"]


def test_budget_curve_skips_budgets_larger_than_data():
    y = np.array([0, 1, 0, 1])
    p = np.array([0.1, 0.9, 0.2, 0.8])
    assert [c["budget"] for c in metrics.budget_curve(y, p, (2, 99))] == [2]


def test_summary_reports_required_keys():
    y = np.array([0, 1, 0, 1])
    p = np.array([0.2, 0.8, 0.3, 0.7])
    out = metrics.summary(y, p, budget=2)
    assert {"pr_auc", "precision_at_k", "recall_at_k", "brier", "lift_at_k",
            "precision", "recall", "threshold", "n_pos", "base_rate"} <= out.keys()
    assert out["precision"] == pytest.approx(1.0)
    assert out["recall"] == pytest.approx(1.0)
