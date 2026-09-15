import duckdb

from .config import (
    EQUIPMENT_STYPES,
    MAX_FAILURE_DURATION_S,
    MAX_GAP_BEFORE_FAILURE_S,
    MIN_FAILURE_DURATION_S,
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
    return f"""
  SELECT ch AS eid, prev_day + INTERVAL 1 DAY AS event_day FROM (
    SELECT ch, day,
           lag(day) OVER (PARTITION BY ch ORDER BY day) AS prev_day,
           date_diff('day', lag(day) OVER (PARTITION BY ch ORDER BY day), day) AS gap_days,
           count(*) OVER (PARTITION BY ch ORDER BY day
                          RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW) AS recent_days
    FROM daily_channel
  ) WHERE gap_days >= 2 AND recent_days >= {min_active}
"""


_SILENCE_SQL = _silence_sql(7)
_SILENCE_STRICT_SQL = _silence_sql(25)


def _in(states: frozenset[str]) -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(states)) + ")"


def _emit(con: duckdb.DuckDBPyConnection, table: str, events_sql: str,
          entity: str, horizon_days: int, base_sql: str | None = None) -> None:
    """Метка = было ли целевое событие в окне (day, day + horizon] для сущности.

    Окно строго будущее: событие в сами сутки day меткой не считается,
    иначе фичи, посчитанные на конец day, увидели бы собственную метку.
    """
    base = base_sql or f"SELECT DISTINCT {entity} AS eid, day FROM daily_channel"
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


def build_fire(con, horizon_days: int = 1, seg_size: float = 10.0) -> None:
    """Пожарный риск участка: объект x корзина пикетов."""
    con.execute(f"""
    CREATE OR REPLACE TABLE label_fire AS
    WITH base AS (
      SELECT DISTINCT obj, CAST(floor(coalesce(picket,0)/{seg_size}) AS INTEGER) AS seg, day
      FROM daily_channel WHERE obj IS NOT NULL
    ), tgt AS (
      SELECT obj, CAST(floor(coalesce(picket,0)/{seg_size}) AS INTEGER) AS seg,
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


def build_intrusion(con, horizon_days: int = 1) -> None:
    _emit(
        con, "label_intrusion",
        "SELECT obj AS eid, day AS event_day FROM daily_channel "
        "WHERE n_intrusion > 0 AND n_alarms > 0 AND obj IS NOT NULL",
        "obj", horizon_days,
        base_sql="SELECT DISTINCT obj AS eid, day FROM daily_channel WHERE obj IS NOT NULL",
    )


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
