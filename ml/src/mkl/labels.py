import duckdb

from .config import (
    EQUIPMENT_STYPES,
    SEG_SIZE,
    EXCLUDED_PERIODS,
    MAX_FAILURE_DURATION_S,
    MAX_GAP_BEFORE_FAILURE_S,
    MIN_FAILURE_DURATION_S,
    seg_sql,
)

# Канал был жив непосредственно перед отказом и отказ не превратился в списание.
_ALIVE = (f"dur_s <= {MAX_FAILURE_DURATION_S} "
          f"AND coalesce(gap_before_s, 0) <= {MAX_GAP_BEFORE_FAILURE_S}")

_VARIANT_FILTER = {
    "L1": "states LIKE '%Неисправен%'",
    "L2": f"dur_s >= {MIN_FAILURE_DURATION_S}",
    "L3": f"dur_s >= {MIN_FAILURE_DURATION_S} AND NOT is_group",
    "L6": f"dur_s >= {MIN_FAILURE_DURATION_S} AND NOT is_group AND {_ALIVE}",
}

# Событием считается ПЕРВЫЕ пропущенные сутки, а не день возобновления работы:
# иначе при горизонте 24 ч метка не может сработать в принципе, ведь разрыв по
# определению длится не меньше двух суток.
#
# min_active — сколько из предыдущих 30 суток канал обязан быть активен, чтобы
# пропуск считался аномалией. При пороге 7 метка ловит штатный редкий опрос:
# канал, отчитывающийся раз в три дня, не отказывает, когда пропускает сутки.
def _silence_sql(min_active: int = 7) -> str:
    """Активность считается на сутках ДО пропажи, а не в день возвращения.

    Прежде окно [день_возврата - 30, день_возврата] целиком накрывалось самим
    простоем, и отбор получался обратным замыслу: чем дольше канал лежал, тем
    меньше был его шанс пройти фильтр. Долю прошедших это давало 0.802 при
    разрыве в сутки, 0.156 при разрыве 7-30 суток и ровно 0.000 при разрыве
    длиннее 30 суток — ноль из 172 995. То есть метка по построению не могла
    содержать ни одного длительного выхода канала из строя, ради которого
    прогноз и заказывался.
    """
    return f"""
  SELECT ch AS eid, prev_day + INTERVAL 1 DAY AS event_day FROM (
    SELECT ch, day,
           lag(day) OVER w AS prev_day,
           date_diff('day', lag(day) OVER w, day) AS gap_days,
           lag(active_30) OVER w AS recent_days
    FROM (
      SELECT ch, day,
             count(*) OVER (PARTITION BY ch ORDER BY day
                            RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW) AS active_30
      FROM daily_channel
    )
    WINDOW w AS (PARTITION BY ch ORDER BY day)
  ) WHERE gap_days >= 2 AND recent_days >= {min_active}
"""


def _silence_rhythm_sql(factor: float = 1.5, min_active: int = 7) -> str:
    """Молчание относительно СОБСТВЕННОГО ритма канала.

    Фиксированный порог «пропуск от двух суток» ловит штатный редкий опрос:
    канал, отчитывающийся раз в три дня, не отказывает, когда пропускает сутки,
    а в метке это позитив. Из 982 438 событий молчания 303 617 — разрывы ровно
    в одни сутки.

    Здесь порог свой у каждого канала: разрыв должен превышать его обычный в
    factor раз. Для ежесуточного канала это по-прежнему двое суток, для
    трёхсуточного — пять. Медиана берётся по предыдущим 30 суткам и снимается
    в строке prev_day, то есть до пропажи.
    """
    return f"""
  SELECT ch AS eid, prev_day + INTERVAL 1 DAY AS event_day FROM (
    SELECT ch, day,
           lag(day) OVER w AS prev_day,
           date_diff('day', lag(day) OVER w, day) AS gap_days,
           lag(active_30) OVER w AS recent_days,
           lag(med_gap_30) OVER w AS med_gap
    FROM (
      SELECT ch, day,
             count(*) OVER r AS active_30,
             median(prev_gap) OVER r AS med_gap_30
      FROM (
        SELECT ch, day,
               date_diff('day', lag(day) OVER (PARTITION BY ch ORDER BY day), day)
                 AS prev_gap
        FROM daily_channel
      )
      WINDOW r AS (PARTITION BY ch ORDER BY day
                   RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW)
    )
    WINDOW w AS (PARTITION BY ch ORDER BY day)
  ) WHERE gap_days >= 2 AND recent_days >= {min_active}
      AND gap_days > {factor} * coalesce(med_gap, 1)
"""


_SILENCE_SQL = _silence_sql(7)
_SILENCE_STRICT_SQL = _silence_sql(25)
_SILENCE_RHYTHM_SQL = _silence_rhythm_sql(1.5)


def _in(states: frozenset[str]) -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(states)) + ")"


def _observable(horizon_days: int) -> str:
    """Условие на сутки, для которых метку вообще можно поставить.

    Отбрасываются двое суток.

    Правый край выборки: для последних horizon_days суток окно (day, day+H]
    выходит за конец данных, и настоящее событие там невидимо — метка молча
    становится отрицательной. На 2026-06-30 это давало 2 716 строк и ровно ноль
    положительных при базовой ставке 0.26. Такие сутки цензурированы, и место им
    не в отрицательном классе, а за пределами выборки.

    Исключённый период: окно суток перед миграцией СМВУ заходит внутрь неё, и
    метка указывает на артефакт. На 2021-03-31 таких положительных было 346.
    Сами сутки миграции отбрасываются при обучении, но их тень через горизонт
    дотягивалась до соседних.
    """
    parts = [f"day + INTERVAL {horizon_days} DAY <= (SELECT max(day) FROM daily_channel)"]
    for a, b in EXCLUDED_PERIODS:
        parts.append(
            f"NOT (day + INTERVAL 1 DAY <= DATE '{b}'"
            f" AND day + INTERVAL {horizon_days} DAY >= DATE '{a}')")
    return " AND ".join(parts)


def _emit(con: duckdb.DuckDBPyConnection, table: str, events_sql: str,
          entity: str, horizon_days: int, base_sql: str | None = None) -> None:
    """Метка = было ли целевое событие в окне (day, day + horizon] для сущности.

    Окно строго будущее: событие в сами сутки day меткой не считается,
    иначе фичи, посчитанные на конец day, увидели бы собственную метку.
    """
    base = base_sql or f"SELECT DISTINCT {entity} AS eid, day FROM daily_channel"
    base = f"SELECT * FROM ({base}) WHERE {_observable(horizon_days)}"
    # Целевые события материализуются отдельной таблицей: коррелированный EXISTS
    # над CTE с оконными функциями DuckDB считает неверно и молча отдаёт ноль.
    con.execute(f"CREATE OR REPLACE TEMP TABLE _tgt AS SELECT DISTINCT eid, event_day FROM ({events_sql})")
    con.execute(f"CREATE OR REPLACE TEMP TABLE _base AS {base}")
    con.execute(f"""
    CREATE OR REPLACE TABLE {table} AS
    SELECT b.eid AS {entity}, b.day,
           CASE WHEN count(t.eid) > 0 THEN 1 ELSE 0 END AS y
    FROM _base b
    LEFT JOIN _tgt t
      ON t.eid = b.eid
     AND t.event_day >  b.day
     AND t.event_day <= b.day + INTERVAL {horizon_days} DAY
    GROUP BY b.eid, b.day
    """)


def build_sensor_failure(con, variant: str = "L3", horizon_days: int = 1,
                         table: str = "label_failure") -> None:
    """L1 любой Неисправен; L2 эпизод >= 1 ч; L3 = L2 без групповых;
    L4 молчание при активности >= 7 из 30 суток; L5 = L6 объединить L4;
    L6 = L3 с требованием, что канал был жив и эпизод не превратился в списание;
    L7 строгое молчание — пропуск у канала, активного >= 25 из 30 суток;
    L8 = L6 объединить L7, то есть отказ или аномальный уход в молчание."""
    if variant == "L4":
        events = _SILENCE_SQL
    elif variant in ("L9", "L9c"):
        events = _SILENCE_RHYTHM_SQL
    elif variant == "L7":
        events = _SILENCE_STRICT_SQL
    elif variant in ("L5", "L8"):
        silence = _SILENCE_SQL if variant == "L5" else _SILENCE_STRICT_SQL
        events = f"""
          SELECT ch AS eid, CAST(t_start AS DATE) AS event_day FROM episodes
          WHERE {_VARIANT_FILTER['L6']}
          UNION ALL
          {silence}
        """
    else:
        events = f"""
          SELECT ch AS eid, CAST(t_start AS DATE) AS event_day FROM episodes
          WHERE {_VARIANT_FILTER[variant]}
        """
    _emit(con, table, events, "ch", horizon_days)
    if variant == "L9c":
        # L9 recognises an anomalous gap only when a later report reveals its
        # length. The last observed row of a channel has no later report: a
        # permanent outage or decommissioning would otherwise be labelled 0.
        # Keep that outcome unknown until an external status or return arrives.
        con.execute(f"""
        DELETE FROM {table} AS l USING (
          SELECT ch, max(day) AS last_day FROM daily_channel GROUP BY ch
        ) AS t
        WHERE l.ch = t.ch AND l.day = t.last_day
        """)


# Цензура для проверки чувствительности A_link к определению события
# (scripts/eval_a_link_availability_checks.py). Правила покрытия выгрузки и
# окна возврата взяты из PR #9 (backend/ml/availability_prepare.py), правило
# выходных — нет: оно проверяет, сколько попаданий A_link приходится на
# предсказуемое молчание с пятницы до понедельника. Продуктовая метка эти
# функции не вызывает: ни build_for_head, ни heads.yaml о них не знают. Приём
# тот же, что у L9c: строка удаляется, и в оценке её исход становится
# unknown, а не нулём.

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


def build_sensor_failure_strict(con, horizon_days: int = 1) -> None:
    """Аномальное поведение конкретного датчика: устойчивый отказ или пропуск
    суток у канала, до того отчитывавшегося 25 из 30 суток.

    Отличается от основной метки редкостью события (около 1,4% против 23%):
    L5 отвечает на вопрос «останется ли канал доступен завтра», а эта метка —
    «какой именно датчик ведёт себя не как обычно», то есть куда ехать бригаде.
    """
    build_sensor_failure(con, variant="L8", horizon_days=horizon_days,
                         table="label_failure_strict")


def build_sensor_degradation(con, horizon_days: int = 1) -> None:
    """Деградация датчика: переход в состояние неисправности в ближайшие сутки.

    ТЗ прямо называет основой прогноза «анализ паттернов ложных сработок и
    частоты шума», а это ровно дребезг `Неисправен`: 1,81 млн мгновенных
    эпизодов из 1,86 млн. Отдельная от устойчивого отказа цель: дребезг —
    повод включить датчик в план обслуживания, отказ — повод выехать сейчас.
    """
    build_sensor_failure(con, variant="L1", horizon_days=horizon_days,
                         table="label_degradation")


def build_group_outage(con, horizon_days: int = 1) -> None:
    _emit(
        con, "label_group_outage",
        "SELECT obj AS eid, CAST(t_start AS DATE) AS event_day FROM group_outages",
        "obj", horizon_days,
        base_sql="SELECT DISTINCT obj AS eid, day FROM daily_channel WHERE obj IS NOT NULL",
    )


def build_fire(con, horizon_days: int = 1, seg_size: float = SEG_SIZE) -> None:
    """Пожарный риск участка: объект x корзина пикетов.

    Участок считается общей формулой из config, и каналы без пикета уходят в
    собственную корзину, а не в нулевую. Прежде подстановка нуля склеивала
    настоящее начало коллектора с беспикетными каналами объекта: 26 007
    участок-суток из 1 074 674 и 228 позитивов из 18 631 относились к слитой
    сущности, а признаки соседей по участку усреднялись по ней же.
    """
    seg = seg_sql("picket", seg_size)
    con.execute(f"""
    CREATE OR REPLACE TABLE label_fire AS
    WITH base AS (
      SELECT DISTINCT obj, {seg} AS seg, day
      FROM daily_channel WHERE obj IS NOT NULL
        AND {_observable(horizon_days)}
    ), tgt AS (
      SELECT obj, {seg} AS seg,
             day AS event_day
      FROM daily_channel WHERE n_fire > 0
    )
    SELECT b.obj, b.seg, b.day,
           CASE WHEN EXISTS (SELECT 1 FROM tgt t
                             WHERE t.obj = b.obj AND t.seg = b.seg
                               AND t.event_day >  b.day
                               AND t.event_day <= b.day + INTERVAL {horizon_days} DAY)
                THEN 1 ELSE 0 END AS y
    FROM base b
    """)


# Состояние охраны объекта с переносом на сутки без событий постановки: объект
# стоит на охране неделями, а событие одно. Перенос строго назад.
_ARMED_SQL = """
  SELECT obj, day,
         last_value(armed IGNORE NULLS) OVER (
           PARTITION BY obj ORDER BY day
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS armed
  FROM (
    SELECT obj, day, arg_max(armed_eod, last_arm_ts)
             FILTER (WHERE armed_eod IS NOT NULL) AS armed
    FROM daily_channel WHERE obj IS NOT NULL GROUP BY obj, day
  )
"""


def build_intrusion(con, horizon_days: int = 1, armed_only: bool = False) -> None:
    """Тревога проникновения на объекте.

    armed_only переопределяет событие: позитивом считается тревога при объекте
    НА ОХРАНЕ. Тревога при снятой охране — это проход персонала, то есть ровно
    санкционированный доступ, и голова, названная «несанкционированный доступ»,
    считала его позитивом наравне с нарушителем. По всей истории состояние
    известно у 86.7% тревог и делит их почти пополам: 52.2% приходится на
    объект под охраной.

    Цена: охрана есть только на 47 объектах из 78, и при armed_only голова
    работает лишь по ним. NULL здесь означает «неизвестно», а не «снято», и
    подставлять ноль нельзя — это превратило бы 31 объект в вечно снятые.
    """
    if not armed_only:
        events = ("SELECT obj AS eid, day AS event_day FROM daily_channel "
                  "WHERE n_intrusion > 0 AND n_alarms > 0 AND obj IS NOT NULL")
        base = ("SELECT DISTINCT obj AS eid, day FROM daily_channel "
                "WHERE obj IS NOT NULL")
    else:
        events = f"""
          SELECT d.obj AS eid, d.day AS event_day
          FROM daily_channel d JOIN ({_ARMED_SQL}) a
            ON a.obj = d.obj AND a.day = d.day
          WHERE d.n_intrusion > 0 AND d.n_alarms > 0 AND d.obj IS NOT NULL
            AND a.armed = 1
        """
        base = f"""
          SELECT DISTINCT obj AS eid, day FROM ({_ARMED_SQL})
          WHERE armed IS NOT NULL
        """
    _emit(con, "label_intrusion", events, "obj", horizon_days, base_sql=base)


def build_wear(con, horizon_days: int = 7) -> None:
    """Износ агрегатов: насосы, вентиляторы, ИБП, люки, датчики затопления."""
    eq = _in(EQUIPMENT_STYPES)
    _emit(
        con, "label_wear",
        f"""
          SELECT e.ch AS eid, CAST(e.t_start AS DATE) AS event_day
          FROM episodes e WHERE e.dur_s >= {MIN_FAILURE_DURATION_S} AND e.stype IN {eq}
          UNION ALL
          SELECT ch AS eid, day AS event_day FROM daily_channel
          WHERE stype IN {eq} AND (n_alarms > 0 OR n_bad > 0)
        """,
        "ch", horizon_days,
        base_sql=f"SELECT DISTINCT ch AS eid, day FROM daily_channel WHERE stype IN {eq}",
    )


def build_flood(con, horizon_days: int = 1) -> None:
    """Риск подтопления объекта.

    Сценарий прямо описан в ТЗ: система сопоставляет частоту включения
    насосных станций с метеоданными и историческими паттернами подтоплений.
    Метка — состояние «Затоплен», которое приходит с каналов насосов.
    Популяция ограничена объектами, где насосы вообще есть: там, где их нет,
    подтопление ничем не измеряется и прогнозировать нечего.
    """
    _emit(
        con, "label_flood",
        "SELECT obj AS eid, day AS event_day FROM daily_channel "
        "WHERE n_flood > 0 AND obj IS NOT NULL",
        "obj", horizon_days,
        base_sql=(
            "SELECT DISTINCT obj AS eid, day FROM daily_channel "
            "WHERE obj IS NOT NULL AND obj IN ("
            "  SELECT DISTINCT obj FROM daily_channel "
            "  WHERE stype = 'Состояние насоса' AND obj IS NOT NULL)"
        ),
    )


# Диспетчер по конфигурации головы. Прежде список построителей дублировался в
# verify_head.py и final_eval.py, а вариант метки головы A брался из файла с
# результатом эксперимента, а не из heads.yaml — два источника правды на одно
# решение.
_BUILDERS = {
    "label_link": lambda con, cfg, h, t: build_sensor_failure(
        con, variant=cfg.get("variant", "L9"), horizon_days=h, table=t),
    "label_failure": lambda con, cfg, h, t: build_sensor_failure(
        con, variant=cfg.get("variant", "L6"), horizon_days=h, table=t),
    "label_failure_strict": lambda con, cfg, h, t: build_sensor_failure_strict(
        con, horizon_days=h),
    "label_degradation": lambda con, cfg, h, t: build_sensor_degradation(
        con, horizon_days=h),
    "label_group_outage": lambda con, cfg, h, t: build_group_outage(con, horizon_days=h),
    "label_fire": lambda con, cfg, h, t: build_fire(con, horizon_days=h),
    "label_intrusion": lambda con, cfg, h, t: build_intrusion(
        con, horizon_days=h, armed_only=bool(cfg.get("armed_only"))),
    "label_wear": lambda con, cfg, h, t: build_wear(con, horizon_days=h),
    "label_flood": lambda con, cfg, h, t: build_flood(con, horizon_days=h),
}


def build_for_head(con: duckdb.DuckDBPyConnection, cfg: dict) -> str:
    """Построить метку головы по её конфигурации. Возвращает имя таблицы."""
    table = cfg["label"]
    _BUILDERS[table](con, cfg, cfg["horizon_days"], table)
    return table
