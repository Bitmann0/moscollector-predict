"""Цензура метки L9c для проверки чувствительности A_link к определению события.

Используется только scripts/eval_a_link_availability_checks.py. Правила покрытия
выгрузки и окна возврата взяты из PR #9 (backend/ml/availability_prepare.py,
коммит 58c1019), правило выходных — нет: оно проверяет, сколько попаданий
A_link приходится на предсказуемое молчание с пятницы до понедельника. Приём
тот же, что у L9c: строка удаляется, и в оценке её исход становится unknown, а
не нулём.

Модуль отделён от labels.py намеренно: labels.py входит в inputs стадии train
(pipeline.py), и любая его правка делает стадию устаревшей, после чего
`mkl run` переобучает модели. Продуктовая метка эти функции не вызывает: ни
build_for_head, ни heads.yaml о них не знают.
"""
import duckdb


def coverage_calendar(con: duckdb.DuckDBPyConnection, min_share: float = 0.5,
                      window_days: int | None = None, by_weekday: bool = True,
                      table: str = "coverage_calendar") -> None:
    """Сколько каналов отчиталось в каждые календарные сутки и провал ли это.

    Календарь строится через generate_series, а не по самой панели: сутки, в
    которые не пришло ни одного канала (2026-06-01), в панели отсутствуют, и
    группировка по ней потеряла бы именно их.

    Правило PR #9 сравнивает сутки с медианой за 30 предыдущих календарных
    суток. Каналов по выходным отчитывается кратно меньше, чем по будням, и
    такое правило отбраковывает в основном субботы и воскресенья, а не сбои
    выгрузки. Поэтому по умолчанию медиана берётся по тем же дням недели за
    window_days // 7 предыдущих недель (56 суток — восемь одноимённых дней);
    by_weekday=False воспроизводит правило PR #9 с окном 30 суток.

    Сутки без единого канала — провал всегда. Сутки без истории для медианы
    провалом не считаются: судить не по чему.
    """
    if not 0 < min_share <= 1:
        raise ValueError("min_share must be in (0, 1]")
    window_days = window_days or (56 if by_weekday else 30)
    if by_weekday:
        weeks = window_days // 7
        if weeks < 1:
            raise ValueError("window_days must cover at least one week")
        median = (f"median(n_channels) OVER (PARTITION BY isodow(day) ORDER BY day "
                  f"ROWS BETWEEN {weeks} PRECEDING AND 1 PRECEDING)")
    else:
        median = (f"median(n_channels) OVER (ORDER BY day "
                  f"ROWS BETWEEN {window_days} PRECEDING AND 1 PRECEDING)")
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE {table} AS
    WITH counts AS (
      SELECT day, count(DISTINCT ch) AS n FROM daily_channel GROUP BY day
    ), cal AS (
      SELECT CAST(g.day AS DATE) AS day, coalesce(c.n, 0) AS n_channels
      FROM (SELECT min(day) AS lo, max(day) AS hi FROM daily_channel) b,
           generate_series(b.lo, b.hi, INTERVAL 1 DAY) AS g(day)
      LEFT JOIN counts c ON c.day = CAST(g.day AS DATE)
    ), med AS (
      SELECT day, n_channels, {median} AS median_channels FROM cal
    )
    SELECT day, n_channels, median_channels,
           n_channels = 0 OR (median_channels IS NOT NULL
                              AND n_channels < {min_share} * median_channels) AS low
    FROM med
    """)


def censor_low_coverage(con: duckdb.DuckDBPyConnection, table: str,
                        horizon_days: int, min_share: float = 0.5,
                        window_days: int | None = None,
                        by_weekday: bool = True) -> int:
    """Удалить строки, чьё окно метки (day, day + H] задевает провал выгрузки.

    В сутки, когда журнал пришёл от малой доли каналов, пропуск почти у всех
    остальных — свойство выгрузки, а не канала, и L9c засчитывает его как
    начало разрыва у каждого. Возвращает число удалённых строк.
    """
    coverage_calendar(con, min_share, window_days, by_weekday, "_coverage")
    return con.execute(f"""
    DELETE FROM {table} AS l USING (SELECT day FROM _coverage WHERE low) AS c
    WHERE c.day > l.day AND c.day <= l.day + INTERVAL {horizon_days} DAY
    """).fetchone()[0]


_NEXT_REPORT_SQL = """
  SELECT ch, day, lead(day) OVER (PARTITION BY ch ORDER BY day) AS next_day
  FROM daily_channel
"""


def censor_unrecovered(con: duckdb.DuckDBPyConnection, table: str,
                       recovery_days: int) -> int:
    """Удалить строку, если следующий отчёт канала позже day + recovery_days.

    Возврат в журнал подтверждает, что пропуск был временным; простой длиннее
    окна возврата может оказаться и отказом, и списанием, и плановым
    отключением, и в PR #9 такой исход считался неизвестным. Строка без
    следующего отчёта удаляется по той же причине. Возвращает число удалённых
    строк.
    """
    if recovery_days < 2:
        # Разрыв длится не меньше двух суток, и при окне в сутки метка теряла
        # бы все позитивы.
        raise ValueError("recovery_days must be at least 2")
    return con.execute(f"""
    DELETE FROM {table} AS l USING ({_NEXT_REPORT_SQL}) AS n
    WHERE l.ch = n.ch AND l.day = n.day
      AND (n.next_day IS NULL
           OR date_diff('day', n.day, n.next_day) > {recovery_days})
    """).fetchone()[0]


def censor_weekend_gap(con: duckdb.DuckDBPyConnection, table: str) -> int:
    """Удалить пятничную строку канала, следующий отчёт которого в понедельник.

    Канал, молчащий по выходным, при ритме «пять суток подряд, потом два дня
    тишины» имеет медиану промежутка в сутки, и L9c считает субботу началом
    разрыва каждую неделю. Если такие строки несут заметную долю попаданий,
    точность A_link частично держится на предсказуемом недельном молчании.
    Возвращает число удалённых строк.
    """
    return con.execute(f"""
    DELETE FROM {table} AS l USING ({_NEXT_REPORT_SQL}) AS n
    WHERE l.ch = n.ch AND l.day = n.day
      AND isodow(l.day) = 5 AND date_diff('day', n.day, n.next_day) = 3
    """).fetchone()[0]
