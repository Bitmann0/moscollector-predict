"""Аудит метки отказа: убедиться, что она ловит реальный отказ, а не артефакт.

Без этого заявленный Precision недоказуем — иначе мы измеряем согласие модели
с собственной эвристикой, а не с действительностью.
"""
import sys

from mkl import db
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

con = db.connect()
db.attach_events(con)
db.attach_parquet(con, "episodes", "group_outages")

sample = con.execute("""
WITH sample AS (
  SELECT ch, obj, stype, t_start, t_end, dur_s, n_events, states
  FROM episodes
  WHERE dur_s >= 3600 AND NOT is_group AND year(t_start) = 2025
  USING SAMPLE 250 ROWS (reservoir, 42)
)
SELECT s.*,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch
          AND e.ts BETWEEN s.t_start - INTERVAL 24 HOUR AND s.t_start) AS ev_24h_before,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch
          AND e.ts BETWEEN s.t_start - INTERVAL 7 DAY
                       AND s.t_start - INTERVAL 24 HOUR) / 6.0 AS ev_daily_baseline,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch AND e.alarm
          AND e.ts BETWEEN s.t_start - INTERVAL 7 DAY AND s.t_start) AS alarms_7d_before,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch
          AND e.val_raw IN ('Неисправен','Неопределен')
          AND e.ts BETWEEN s.t_start - INTERVAL 7 DAY AND s.t_start) AS bad_7d_before
FROM sample s ORDER BY dur_s DESC
""").df()

out = PATHS.reports / "label_audit.csv"
sample.to_csv(out, index=False, encoding="utf-8")
print(f"выборка записана: {out}  ({len(sample)} строк)\n")

print("=== сводка по выборке ===")
print(con.execute("""
WITH s AS (SELECT * FROM read_csv(?, header=true))
SELECT count(*) AS n,
       count(*) FILTER (WHERE dur_s >= 86400) AS дольше_суток,
       round(median(dur_s) / 3600, 1) AS медиана_часов,
       count(*) FILTER (WHERE ev_24h_before < 0.5 * ev_daily_baseline) AS активность_упала,
       count(*) FILTER (WHERE ev_24h_before > 2.0 * ev_daily_baseline) AS активность_выросла,
       count(*) FILTER (WHERE alarms_7d_before > 0) AS были_тревоги_за_7сут,
       count(*) FILTER (WHERE bad_7d_before > 0)    AS были_сбои_за_7сут
FROM s
""", [str(out)]).df().to_string(index=False))

print("\n=== по типам датчиков ===")
print(con.execute("""
SELECT stype, count(*) AS n, round(median(dur_s)/3600, 1) AS медиана_часов
FROM read_csv(?, header=true) GROUP BY 1 ORDER BY n DESC
""", [str(out)]).df().to_string(index=False))

print("\n=== первые 15 эпизодов ===")
cols = ["ch", "stype", "t_start", "dur_s", "ev_24h_before",
        "ev_daily_baseline", "alarms_7d_before", "bad_7d_before"]
print(sample[cols].head(15).to_string(index=False))
con.close()
