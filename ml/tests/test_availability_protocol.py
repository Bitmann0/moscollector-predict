"""Цензура метки L9c для проверки чувствительности A_link (PR #9).

Сценарии перенесены из tests/test_availability_prepare.py PR #9, но на
продакшн-схеме суточной панели. L9c считает активность канала по самой
панели, поэтому каждому каналу нужна неделя предыстории: без неё
recent_days < 7, и разрыв меткой не становится.
"""
import datetime as dt

import polars as pl
import pytest
from conftest import insert_day

from mkl import cv, label_censoring, labels

D0 = dt.date(2025, 1, 1)  # среда


def _days(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def _fill(con, ch: int, days) -> None:
    for d in days:
        insert_day(con, ch=ch, day=d.isoformat())


def _link(con) -> None:
    labels.build_sensor_failure(con, variant="L9c", horizon_days=1,
                                table="label_link")


def _rows(con, where: str = "TRUE") -> set[tuple[int, dt.date]]:
    return set(con.execute(
        f"SELECT ch, day FROM label_link WHERE {where}").fetchall())


def test_low_coverage_day_censors_only_the_day_before(daily_con):
    """В среду 05.02 отчитался один канал из десяти. Пропуск у остальных девяти
    L9c засчитывает началом разрыва, хотя это провал выгрузки."""
    bad = dt.date(2025, 2, 5)
    for ch in range(1, 11):
        _fill(daily_con, ch, [d for d in _days(D0, dt.date(2025, 2, 10))
                              if ch == 1 or d != bad])
    _link(daily_con)
    before = bad - dt.timedelta(days=1)
    assert daily_con.execute(
        "SELECT count(*), sum(y) FROM label_link WHERE day = ?", [before]
    ).fetchone() == (10, 9)
    deleted = label_censoring.censor_low_coverage(daily_con, "label_link", horizon_days=1)
    assert deleted == 10
    assert _rows(daily_con, f"day = DATE '{before}'") == set()
    assert _rows(daily_con, f"day = DATE '{bad}'") == {(1, bad)}
    for d in (before - dt.timedelta(days=1), bad + dt.timedelta(days=1)):
        assert len(_rows(daily_con, f"day = DATE '{d}'")) == 10


def test_day_without_any_channel_is_low_coverage(daily_con):
    """Сутки, в которые не пришло ни одного канала, в панели отсутствуют.
    Календарь обязан их содержать, иначе самый явный провал не виден."""
    gap = dt.date(2025, 1, 20)
    for ch in range(1, 4):
        _fill(daily_con, ch, [d for d in _days(D0, dt.date(2025, 1, 31)) if d != gap])
    label_censoring.coverage_calendar(daily_con)
    row = daily_con.execute(
        "SELECT n_channels, low FROM coverage_calendar WHERE day = ?", [gap]
    ).fetchone()
    assert row == (0, True)
    assert daily_con.execute(
        "SELECT count(*) FROM coverage_calendar WHERE low").fetchone() == (1,)
    _link(daily_con)
    label_censoring.censor_low_coverage(daily_con, "label_link", horizon_days=1)
    assert _rows(daily_con, f"day = DATE '{gap - dt.timedelta(days=1)}'") == set()


def test_day_without_any_channel_is_low_even_without_history(daily_con):
    """Первый четверг календаря: медианы того же дня недели ещё нет, но сутки
    без единого канала — провал всегда."""
    thu = D0 + dt.timedelta(days=1)
    for ch in range(1, 4):
        _fill(daily_con, ch, [d for d in _days(D0, dt.date(2025, 1, 31)) if d != thu])
    label_censoring.coverage_calendar(daily_con)
    assert daily_con.execute(
        "SELECT n_channels, median_channels, low FROM coverage_calendar WHERE day = ?",
        [thu]).fetchone() == (0, None, True)


def test_weekday_rule_keeps_weekends_that_pr9_rule_drops(daily_con):
    """Восемь каналов отчитываются только по будням, два — каждый день.

    По выходным приходит 2 канала из 10. Правило PR #9 (медиана за 30
    предыдущих суток) считает каждую субботу и воскресенье провалом выгрузки и
    снимает пятничные и субботние строки. По медиане того же дня недели
    выходные нормальны, и не снимается ничего.
    """
    days = _days(D0, D0 + dt.timedelta(days=41))
    for ch in range(1, 9):
        _fill(daily_con, ch, [d for d in days if d.isoweekday() <= 5])
    for ch in (9, 10):
        _fill(daily_con, ch, days)
    _link(daily_con)
    total = daily_con.execute("SELECT count(*) FROM label_link").fetchone()[0]

    assert label_censoring.censor_low_coverage(daily_con, "label_link", 1,
                                      by_weekday=True) == 0

    label_censoring.coverage_calendar(daily_con, by_weekday=False)
    low = [r[0] for r in daily_con.execute(
        "SELECT day FROM coverage_calendar WHERE low ORDER BY day").fetchall()]
    assert low == [d for d in days if d.isoweekday() >= 6]
    deleted = label_censoring.censor_low_coverage(daily_con, "label_link", 1,
                                         by_weekday=False)
    assert deleted > 0
    left = daily_con.execute("SELECT count(*) FROM label_link").fetchone()[0]
    assert left == total - deleted
    assert daily_con.execute(
        "SELECT count(*) FROM label_link WHERE isodow(day) IN (5, 6)"
    ).fetchone() == (0,)


def test_unrecovered_gap_is_censored(daily_con):
    """Сценарий test_strict_availability_target_requires_recovery PR #9,
    сдвинутый на неделю предыстории.

    ch1 [1..8, 10, 11] — разрыв в сутки с возвратом, позитив на 8-е; ch2
    [1..8, 16] — возврат через 8 суток, при окне 7 строка 8-го удаляется;
    ch3 и ch4 без разрывов — только отрицательные.
    """
    jan = [D0 + dt.timedelta(days=i) for i in range(16)]
    _fill(daily_con, 1, jan[:8] + [jan[9], jan[10]])
    _fill(daily_con, 2, jan[:8] + [jan[15]])
    _fill(daily_con, 3, jan[:11])
    _fill(daily_con, 4, jan)
    # ch5 возвращается ровно через 7 суток: это ещё в окне возврата.
    _fill(daily_con, 5, jan[:8] + [jan[14], jan[15]])
    _link(daily_con)
    day8 = jan[7]
    assert _rows(daily_con, "y = 1") == {(1, day8), (2, day8), (5, day8)}

    assert label_censoring.censor_unrecovered(daily_con, "label_link", recovery_days=7) == 1
    assert _rows(daily_con, "y = 1") == {(1, day8), (5, day8)}
    assert (2, day8) not in _rows(daily_con)
    assert {(2, d) for d in jan[:7]} <= _rows(daily_con, "y = 0")
    assert _rows(daily_con, "ch IN (3, 4) AND y = 1") == set()


def test_recovery_window_shorter_than_a_gap_is_rejected(daily_con):
    with pytest.raises(ValueError, match="at least 2"):
        label_censoring.censor_unrecovered(daily_con, "label_link", recovery_days=1)


def test_weekend_gap_drops_only_friday_with_monday_return(daily_con):
    """Удаляется пятничная строка с возвратом в понедельник и ничего больше:
    ни пятница с возвратом во вторник, ни четверг с возвратом в понедельник,
    ни вторник с возвратом в пятницу, ни пятница ежедневного канала."""
    feb = dt.date(2025, 2, 14)
    # ch1 — только будни, 06.01 (пн) … 31.01 (пт).
    _fill(daily_con, 1, [d for d in _days(dt.date(2025, 1, 6), dt.date(2025, 1, 31))
                         if d.isoweekday() <= 5])
    # ch2 — ежедневно, но пт 17.01 -> вт 21.01 и чт 23.01 -> пн 27.01.
    _fill(daily_con, 2, [d for d in _days(D0, feb)
                         if d not in _days(dt.date(2025, 1, 18), dt.date(2025, 1, 20))
                         and d not in _days(dt.date(2025, 1, 24), dt.date(2025, 1, 26))])
    # ch3 — ежедневно без пропусков.
    _fill(daily_con, 3, _days(D0, feb))
    # ch4 — ежедневно, но вт 28.01 -> пт 31.01: тот же трёхдневный промежуток не с пятницы.
    _fill(daily_con, 4, [d for d in _days(D0, feb)
                         if d not in (dt.date(2025, 1, 29), dt.date(2025, 1, 30))])
    _link(daily_con)
    before = _rows(daily_con)

    deleted = label_censoring.censor_weekend_gap(daily_con, "label_link")
    removed = before - _rows(daily_con)
    fridays = {dt.date(2025, 1, 10), dt.date(2025, 1, 17), dt.date(2025, 1, 24)}
    assert removed == {(1, d) for d in fridays}
    assert deleted == 3


def test_script_variants_and_audit_on_synthetic_panel(daily_con):
    """Скрипт строит все варианты на одной панели, и счётчики аудита сходятся
    с тем, что сняла каждая цензура."""
    from scripts.eval_a_link_availability_checks import (BASE, VARIANTS,
                                                          build_labels,
                                                          label_audit)

    days = _days(D0, dt.date(2025, 10, 31))
    for ch in range(1, 9):
        _fill(daily_con, ch, [d for d in days if d.isoweekday() <= 5])
    for ch in (9, 10):
        _fill(daily_con, ch, days)
    cfg = {"label": "label_link", "variant": "L9c", "horizon_days": 1}
    labs = build_labels(daily_con, cfg, list(VARIANTS), days[0], days[-1])
    assert list(labs) == list(VARIANTS) and next(iter(labs)) == BASE
    splits = cv.walk_forward(sorted(labs[BASE]["day"].unique().to_list()),
                             2, 60, 31)
    audit = label_audit(daily_con, labs, splits, 1, days[0])
    base = labs[BASE]
    for name, lab in labs.items():
        span = audit["variants"][name]["span"]
        assert span["rows"] == lab.height
        assert span["censored_rows"] == base.height - lab.height
        assert span["positives"] == lab.filter(pl.col("y") == 1).height
    # У будничных каналов каждая пятница — позитив L9c, и именно её снимает
    # правило выходных; правило PR #9 снимает ещё и субботы ежедневных.
    weekend = audit["variants"]["no_friday_monday"]["span"]
    assert weekend["censored_positives"] > 0
    assert weekend["censored_positives"] == \
        weekend["censored_positives_by_first_silent_weekday"]["sat"]
    assert audit["variants"]["coverage_weekday"]["span"]["censored_rows"] == 0
    assert audit["variants"]["coverage_30d_pr9"]["span"]["censored_rows"] > 0
    cov = audit["coverage"]
    assert cov["weekday_8w"]["all"]["low_days"] == 0
    assert cov["trailing_30d_pr9"]["all"]["low_days_by_weekday"]["mon"] == 0
    assert cov["median_channels_by_weekday_since_start"]["sat"] == 2
