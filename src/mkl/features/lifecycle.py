import duckdb


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
