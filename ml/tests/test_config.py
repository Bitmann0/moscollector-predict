from datetime import date

from mkl import config


def test_state_vocabularies_are_disjoint():
    assert not (config.BAD_STATES & config.OK_STATES)
    assert not (config.FIRE_STATES & config.INTRUSION_STATES)


def test_holdout_is_2026_h1():
    assert config.HOLDOUT_START == date(2026, 1, 1)
    assert config.HOLDOUT_END == date(2026, 6, 30)


def test_embargo_covers_horizon_plus_window():
    assert config.EMBARGO_DAYS >= 1 + config.MAX_FEATURE_WINDOW_DAYS
    assert config.EMBARGO_DAYS_WEAR >= 7 + config.MAX_FEATURE_WINDOW_DAYS
