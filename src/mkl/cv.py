import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Split:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date

    @property
    def gap_days(self) -> int:
        return (self.test_start - self.train_end).days


def live_windows(last: dt.date, embargo_days: int) -> dict[str, dt.date]:
    """Rolling calibration and threshold windows for the deployed model."""
    threshold_start = last - dt.timedelta(days=29)
    calibration_end = threshold_start - dt.timedelta(days=1)
    calibration_start = calibration_end - dt.timedelta(days=29)
    training_end = calibration_start - dt.timedelta(days=embargo_days + 1)
    return {"training_end": training_end,
            "calibration_start": calibration_start,
            "calibration_end": calibration_end,
            "threshold_start": threshold_start,
            "threshold_end": last}


def walk_forward(days: list[dt.date], n_splits: int, test_days: int,
                 embargo_days: int, min_train_days: int = 90) -> list[Split]:
    """Rolling origin с purge и embargo.

    Между концом обучения и началом теста оставляется разрыв в embargo_days
    суток. Он обязан покрывать горизонт метки плюс максимальное окно фич,
    иначе обучающая строка и тестовая метка смотрят на одни и те же события.
    """
    days = sorted(set(days))
    if not days:
        raise ValueError("пустой список суток")
    first, last = days[0], days[-1]
    needed = min_train_days + embargo_days + test_days * n_splits
    span = (last - first).days + 1
    if span < needed:
        raise ValueError(f"истории {span} суток, нужно минимум {needed}")

    splits: list[Split] = []
    for i in range(n_splits, 0, -1):
        test_end = last - dt.timedelta(days=test_days * (i - 1))
        test_start = test_end - dt.timedelta(days=test_days - 1)
        train_end = test_start - dt.timedelta(days=embargo_days + 1)
        splits.append(Split(first, train_end, test_start, test_end))
    return splits
