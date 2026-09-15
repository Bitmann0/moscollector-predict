"""Аудит метки отказа: убедиться, что она ловит реальный отказ, а не артефакт.

Без этого заявленный Precision недоказуем — иначе мы измеряем согласие модели
с собственной эвристикой, а не с действительностью.
"""
import sys

import polars as pl

from mkl import db
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

con = db.connect()
db.attach_events(con)
db.attach_parquet(con, "episodes", "group_outages")

con.execute("""
CREATE TABLE sample AS
SELECT * FROM (
  SELECT ch, obj, stype, t_start, t_end, dur_s, n_events, states
  FROM episodes
  WHERE dur_s >= 3600 AND NOT is_group AND year(t_start) = 2025
) ORDER BY hash(ch * 1000003 + CAST(epoch(t_start) AS BIGINT)) LIMIT 250
""")

df = con.execute("""
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
""").pl()

out = PATHS.reports / "label_audit.csv"
df.write_csv(out)
print(f"выборка записана: {out}  ({df.height} строк)\n")

if df.height:
    base = pl.col("ev_daily_baseline")
    before = pl.col("ev_24h_before")
    summary = df.select([
        pl.len().alias("n"),
        (pl.col("dur_s") >= 86400).sum().alias("дольше_суток"),
        (pl.col("dur_s").median() / 3600).round(1).alias("медиана_часов"),
        ((before < 0.5 * base) & (base > 0)).sum().alias("активность_упала"),
        ((before > 2.0 * base) & (base > 0)).sum().alias("активность_выросла"),
        (pl.col("alarms_7d_before") > 0).sum().alias("тревоги_за_7сут"),
        (pl.col("bad_7d_before") > 0).sum().alias("сбои_за_7сут"),
        (base == 0).sum().alias("канал_молчал_неделю"),
    ])
    print("=== сводка по выборке ===")
    print(summary.to_pandas().to_string(index=False))

    print("\n=== по типам датчиков ===")
    print(df.group_by("stype")
            .agg([pl.len().alias("n"),
                  (pl.col("dur_s").median() / 3600).round(1).alias("медиана_часов")])
            .sort("n", descending=True)
            .to_pandas().to_string(index=False))

    print("\n=== первые 15 эпизодов ===")
    cols = ["ch", "stype", "t_start", "dur_s", "ev_24h_before",
            "ev_daily_baseline", "alarms_7d_before", "bad_7d_before"]
    print(df.select(cols).head(15).to_pandas().to_string(index=False))

con.close()
