import duckdb

from .config import (BAD_STATES, GROUP_OUTAGE_MIN_CHANNELS, GROUP_OUTAGE_WINDOW_MIN,
                     MIN_FAILURE_DURATION_S)


def _bad_sql() -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(BAD_STATES)) + ")"


def build_episodes(con: duckdb.DuckDBPyConnection, source: str = "ev") -> None:
    """Эпизод — непрерывный прогон событий канала в BAD_STATES до первого события вне них."""
    con.execute(f"""
    CREATE OR REPLACE TABLE episodes AS
    WITH s AS (
      SELECT ch, obj, stype, ts, val_raw,
             CASE WHEN val_raw IN {_bad_sql()} THEN 1 ELSE 0 END AS bad,
             lag(ts) OVER (PARTITION BY ch ORDER BY ts) AS prev_ts
      FROM {source}
      WHERE val_num IS NULL
    ), g AS (
      SELECT *,
             row_number() OVER (PARTITION BY ch ORDER BY ts)
           - row_number() OVER (PARTITION BY ch, bad ORDER BY ts) AS grp
      FROM s
    )
    SELECT ch,
           any_value(obj)   AS obj,
           any_value(stype) AS stype,
           min(ts)          AS t_start,
           max(ts)          AS t_end,
           CAST(epoch(max(ts)) - epoch(min(ts)) AS BIGINT) AS dur_s,
           count(*)         AS n_events,
           string_agg(DISTINCT val_raw, '|') AS states,
           -- разрыв до предыдущего события канала: если он огромен, канал уже
           -- спал и эпизод отражает не отказ, а дремлющий или списанный канал
           CAST(epoch(min(ts)) - epoch(arg_min(prev_ts, ts)) AS BIGINT) AS gap_before_s,
           false            AS is_group
    FROM g WHERE bad = 1
    GROUP BY ch, grp
    """)


def build_group_outages(con: duckdb.DuckDBPyConnection) -> None:
    """Окно, в котором одновременно легло >= GROUP_OUTAGE_MIN_CHANNELS каналов объекта.

    Считаются только устойчивые эпизоды (>= MIN_FAILURE_DURATION_S). Без этого
    условия 93.5% строк episodes — мгновенные пометки нулевой длительности, и
    групповой отказ вырождался в кластер дребезга: из 201 036 окон, признанных
    групповыми, лишь 7.31% содержали хотя бы один эпизод длиннее часа.

    Цена ошибки была двойной, потому что is_group входит в фильтр L3/L6: метка
    «одиночный отказ» теряла 78% настоящих отказов (15 747 из 71 725), и именно
    это загнало базу головы A в 0.002 и заставило подмешивать к ней молчание.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE group_outages AS
    WITH bucketed AS (
      SELECT obj,
             CAST(epoch(t_start) AS BIGINT) // ({GROUP_OUTAGE_WINDOW_MIN} * 60) AS bucket,
             ch, t_start
      FROM episodes
      WHERE obj IS NOT NULL AND dur_s >= {MIN_FAILURE_DURATION_S}
    )
    SELECT obj, bucket,
           min(t_start) AS t_start, max(t_start) AS t_end,
           count(DISTINCT ch) AS n_channels
    FROM bucketed
    GROUP BY obj, bucket
    HAVING count(DISTINCT ch) >= {GROUP_OUTAGE_MIN_CHANNELS}
    """)


def mark_group_episodes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE episodes AS
    SELECT e.* REPLACE (
      EXISTS (
        SELECT 1 FROM group_outages o
        WHERE o.obj = e.obj
          AND CAST(epoch(e.t_start) AS BIGINT) // ({GROUP_OUTAGE_WINDOW_MIN} * 60) = o.bucket
      ) AS is_group
    )
    FROM episodes e
    """)


def build_all(con: duckdb.DuckDBPyConnection, source: str = "ev") -> None:
    build_episodes(con, source=source)
    build_group_outages(con)
    mark_group_episodes(con)
