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


def live_threshold_end(source_end: dt.date, horizon_days: int,
                       embargo_days: int) -> dt.date:
    """Конец окна порога у модели, обученной на данных по source_end включительно.

    Последний день с меткой — source_end минус горизонт: окно (day, day + H]
    обязано целиком лежать в данных (labels._observable). Окна дальше строит
    live_windows, как в train_latest.py.
    """
    last_label = source_end - dt.timedelta(days=horizon_days)
    return live_windows(last_label, embargo_days)["threshold_end"]


def window_plan(first: dt.date, last: dt.date, horizon_days: int,
                embargo_days: int, max_lag_days: int) -> list[dict[str, dt.date]]:
    """Наименьший набор отсечек source_end, при котором у каждого дня first..last
    есть модель с задержкой 0 < day - threshold_end <= max_lag_days.

    Жадно: первому непокрытому дню даётся самая свежая допустимая модель, у
    которой окно порога кончается накануне; она покрывает max_lag_days суток
    подряд, а меньше моделей при таком покрытии не бывает.
    """
    if max_lag_days < 1:
        raise ValueError("max_lag_days must be positive")
    if first > last:
        raise ValueError("empty window")
    plan = []
    day = first
    while day <= last:
        source_end = day - dt.timedelta(days=1) + dt.timedelta(days=horizon_days)
        threshold_end = live_threshold_end(source_end, horizon_days, embargo_days)
        valid_to = threshold_end + dt.timedelta(days=max_lag_days)
        plan.append({"source_end": source_end, "threshold_end": threshold_end,
                     "valid_from": threshold_end + dt.timedelta(days=1),
                     "valid_to": valid_to})
        day = valid_to + dt.timedelta(days=1)
    return plan


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
