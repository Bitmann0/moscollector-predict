import sys
import time

from mkl import db, panel
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

t0 = time.time()
con = db.connect()
db.attach_events(con)
panel.build_daily_channel(con)
con.execute(
    f"COPY daily_channel TO '{PATHS.interim / 'daily_channel.parquet'}' "
    "(FORMAT PARQUET, COMPRESSION ZSTD)"
)
print(con.execute("""
  SELECT count(*) AS rows, count(DISTINCT ch) AS ch, min(day) AS d_min, max(day) AS d_max
  FROM daily_channel
""").df().to_string(index=False))
print(f"{time.time() - t0:.0f}s")
con.close()
