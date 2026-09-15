import duckdb

from ..config import MAX_FAILURE_DURATION_S, MIN_FAILURE_DURATION_S


def add_episode_history(con: duckdb.DuckDBPyConnection,
                        source: str = "feat_full") -> None:
    """История ЗАВЕРШЁННЫХ отказных эпизодов канала.

    Не циркулярно: считаются только эпизоды, закончившиеся не позже текущих
    суток, а метка спрашивает про эпизоды, начинающиеся строго позже. Канал,
    отказывавший пять раз за год, заметно вероятнее откажет снова — без этого
    признака модель вынуждена выводить склонность к отказам из сырых счётчиков.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE _ep_ends AS
    SELECT ch, CAST(t_end AS DATE) AS day, count(*) AS n_ep
    FROM episodes
    WHERE dur_s >= {MIN_FAILURE_DURATION_S} AND dur_s <= {MAX_FAILURE_DURATION_S}
      AND NOT is_group
    GROUP BY ch, CAST(t_end AS DATE)
    """)
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_ephist AS
    WITH j AS (
      SELECT f.*, coalesce(e.n_ep, 0) AS ep_today
      FROM {source} f
      LEFT JOIN _ep_ends e ON e.ch = f.ch AND e.day = f.day
    )
    SELECT * EXCLUDE (ep_today),
           sum(ep_today) OVER hist AS n_prior_episodes,
           date_diff('day',
             max(CASE WHEN ep_today > 0 THEN day END) OVER hist,
             day) AS days_since_prior_episode,
           CASE WHEN age_days > 30
                THEN 365.0 * sum(ep_today) OVER hist / age_days
                END AS episodes_per_year,
           CASE WHEN age_days > 30
                THEN 365.0 * n_prior_failures / age_days
                END AS bad_days_per_year
    FROM j
    WINDOW hist AS (PARTITION BY ch ORDER BY day
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    """)


def add_weekday_profile(con: duckdb.DuckDBPyConnection,
                        source: str = "feat_ephist") -> None:
    """Профиль отчётности канала по дням недели.

    Часть каналов штатно опрашивается не каждый день, и пропуск понедельника
    у такого канала — норма, а не отказ. Голый признак дня недели этого не
    различает: нужна доля тех же дней недели в прошлом, когда канал был
    активен. Счёт накопительный и строго прошлый.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_dow AS
    WITH f AS (SELECT ch, min(day) AS fs FROM {source} GROUP BY ch),
         j AS (
           SELECT s.*, dayofweek(s.day) AS _dow, f.fs
           FROM {source} s JOIN f ON f.ch = s.ch
         )
    SELECT * EXCLUDE (_dow, fs),
           CAST(row_number() OVER (PARTITION BY ch, _dow ORDER BY day) AS DOUBLE)
             / (date_diff('day', fs, day) / 7 + 1) AS dow_active_rate,
           date_diff('day',
             lag(day) OVER (PARTITION BY ch, _dow ORDER BY day), day)
             AS days_since_same_dow,
           CAST(count(*) OVER (PARTITION BY ch, _dow ORDER BY day
                RANGE BETWEEN INTERVAL 83 DAY PRECEDING AND CURRENT ROW) AS DOUBLE)
             / 12.0 AS dow_active_rate_w12
    FROM j
    """)


def add_lifecycle_features(con: duckdb.DuckDBPyConnection,
                           source: str = "feat_spatial") -> None:
    """Возраст и износ.

    Дат ввода в эксплуатацию в выгрузке нет, поэтому возраст подменяется датой
    первого появления канала в журнале, а интенсивность эксплуатации —
    накопленным числом событий и числом переключений (для насосов и
    вентиляторов это буквально циклы пуск/останов).
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_full AS
    WITH first_seen AS (SELECT ch, min(day) AS fs FROM {source} GROUP BY ch)
    SELECT s.*,
           date_diff('day', f.fs, s.day) AS age_days,
           sum(s.n_events) OVER lifetime      AS cum_events,
           sum(s.n_alarms) OVER lifetime      AS cum_alarms,
           sum(CASE WHEN s.n_bad > 0 THEN 1 ELSE 0 END) OVER lifetime AS n_prior_failures,
           sum(s.n_transitions) OVER w30      AS n_duty_cycles_w30,
           sum(s.n_transitions) OVER lifetime AS cum_duty_cycles,
           date_diff('day',
             max(CASE WHEN s.n_bad > 0 THEN s.day END) OVER lifetime,
             s.day) AS days_since_prior_failure
    FROM {source} s
    JOIN first_seen f ON f.ch = s.ch
    WINDOW
      lifetime AS (PARTITION BY s.ch ORDER BY s.day
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
      w30 AS (PARTITION BY s.ch ORDER BY s.day
              RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW)
    """)
