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


def test_summary_reports_required_keys():
    y = np.array([0, 1, 0, 1])
    p = np.array([0.2, 0.8, 0.3, 0.7])
    out = metrics.summary(y, p, budget=2)
    assert {"pr_auc", "precision_at_k", "recall_at_k", "brier", "lift_at_k",
            "precision", "recall", "threshold", "n_pos", "base_rate"} <= out.keys()
    assert out["precision"] == pytest.approx(1.0)
    assert out["recall"] == pytest.approx(1.0)
