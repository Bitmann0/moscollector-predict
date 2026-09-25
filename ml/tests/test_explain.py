"""Объяснение конкретного алерта.

Важность признаков по gain отвечает на другой вопрос — какие признаки модель
использует вообще. Для строки она бесполезна.
"""
import numpy as np
import pytest

from mkl import explain


@pytest.fixture(scope="module")
def model_and_data():
    import xgboost as xgb
    rng = np.random.default_rng(0)
    X = rng.random((400, 5)).astype("float32")
    y = (X[:, 0] > 0.7).astype(int)
    m = xgb.XGBClassifier(n_estimators=40, tree_method="hist", verbosity=0)
    m.fit(X, y)
    return m, X


NAMES = ["n_intrusion", "n_alarms", "dow", "age_days", "безымянный"]


def test_driving_feature_appears_in_the_explanation(model_and_data):
    m, X = model_and_data
    hi = X[np.argsort(-X[:, 0])[:1]]
    got = explain.contributions(m, hi, NAMES, top=3)[0]
    assert got, "у строки высокого риска обязан быть хоть один фактор"
    assert got[0]["feature"] == "n_intrusion"


def test_low_risk_row_gets_no_invented_reasons(model_and_data):
    """Выдумывать причину там, где вверх ничто не толкает, хуже, чем промолчать."""
    m, X = model_and_data
    lo = X[np.argsort(X[:, 0])[:1]]
    assert explain.contributions(m, lo, NAMES, top=3)[0] == []


def test_only_upward_contributions_are_reported(model_and_data):
    m, X = model_and_data
    for row in explain.contributions(m, X[:20], NAMES, top=5):
        assert all(f["contribution"] > 0 for f in row)


def test_feature_without_translation_is_shown_as_is():
    """Лучше непонятное имя, чем выдуманное объяснение."""
    assert explain.label("безымянный") == "безымянный"
    assert explain.label("n_intrusion") != "n_intrusion"


def test_empty_input_does_not_crash(model_and_data):
    m, _ = model_and_data
    assert explain.contributions(m, np.empty((0, 5), dtype="float32"), NAMES) == []
