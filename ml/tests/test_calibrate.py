import numpy as np

from mkl import calibrate


def test_isotonic_improves_brier_on_miscalibrated_scores():
    rng = np.random.default_rng(0)
    y = (rng.random(2000) < 0.3).astype(int)
    raw = np.clip(y * 0.5 + rng.normal(0, 0.2, 2000) + 0.25, 0, 1)
    skewed = raw ** 3
    iso = calibrate.fit_isotonic(skewed, y)
    cal = calibrate.apply(iso, skewed)
    assert np.mean((cal - y) ** 2) < np.mean((skewed - y) ** 2)


def test_apply_keeps_values_in_unit_interval():
    rng = np.random.default_rng(1)
    y = (rng.random(500) < 0.5).astype(int)
    p = rng.random(500)
    iso = calibrate.fit_isotonic(p, y)
    out = calibrate.apply(iso, np.array([-5.0, 0.0, 0.5, 1.0, 5.0]))
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_calibration_preserves_ranking_order():
    rng = np.random.default_rng(2)
    y = (rng.random(1000) < 0.2).astype(int)
    p = np.clip(y * 0.4 + rng.normal(0, 0.2, 1000) + 0.2, 0, 1)
    iso = calibrate.fit_isotonic(p, y)
    grid = np.linspace(0, 1, 50)
    cal = calibrate.apply(iso, grid)
    assert np.all(np.diff(cal) >= -1e-12), "изотоника обязана быть неубывающей"
