import datetime as dt

import pytest

from mkl import cv


def _days(n, start=dt.date(2024, 1, 1)):
    return [start + dt.timedelta(days=i) for i in range(n)]


def test_embargo_gap_is_respected():
    splits = cv.walk_forward(_days(400), n_splits=3, test_days=30, embargo_days=31)
    for s in splits:
        assert s.gap_days > 31, f"разрыв {s.gap_days} суток не покрывает embargo"


def test_train_always_precedes_test():
    for s in cv.walk_forward(_days(400), n_splits=3, test_days=30, embargo_days=31):
        assert s.train_end < s.test_start
        assert s.train_start < s.train_end
        assert s.test_start <= s.test_end


def test_test_windows_do_not_overlap():
    splits = cv.walk_forward(_days(400), n_splits=3, test_days=30, embargo_days=31)
    for a, b in zip(splits, splits[1:]):
        assert a.test_end < b.test_start


def test_last_split_ends_at_last_day():
    days = _days(400)
    splits = cv.walk_forward(days, n_splits=3, test_days=30, embargo_days=31)
    assert splits[-1].test_end == days[-1]


def test_raises_when_history_too_short():
    with pytest.raises(ValueError, match="нужно минимум"):
        cv.walk_forward(_days(40), n_splits=3, test_days=30, embargo_days=31)


def test_raises_on_empty_input():
    with pytest.raises(ValueError, match="пустой"):
        cv.walk_forward([], n_splits=1, test_days=30, embargo_days=31)


def test_wear_embargo_is_wider():
    splits = cv.walk_forward(_days(500), n_splits=2, test_days=30, embargo_days=37)
    for s in splits:
        assert s.gap_days > 37
