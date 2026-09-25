import sys
import time

from mkl import db, states
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

t0 = time.time()
con = db.connect()
db.attach_events(con)
states.build_all(con)
con.execute(f"COPY episodes TO '{PATHS.interim / 'episodes.parquet'}' (FORMAT PARQUET)")
con.execute(f"COPY group_outages TO '{PATHS.interim / 'group_outages.parquet'}' (FORMAT PARQUET)")

print(con.execute("""
  SELECT is_group,
         count(*) AS episodes,
         count(DISTINCT ch) AS channels,
         count(*) FILTER (WHERE dur_s >= 3600) AS long_episodes
  FROM episodes GROUP BY 1 ORDER BY 1
""").df().to_string(index=False))

print("\nдлинные одиночные эпизоды по годам:")
print(con.execute("""
  SELECT year(t_start) AS y, count(*) AS n, count(DISTINCT ch) AS ch
  FROM episodes WHERE dur_s >= 3600 AND NOT is_group
  GROUP BY 1 ORDER BY 1
""").df().to_string(index=False))

print(f"\n{time.time() - t0:.0f}s")
con.close()
