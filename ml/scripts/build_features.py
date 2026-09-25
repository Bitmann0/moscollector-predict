import sys
import time

from mkl import db, store
from mkl.config import PATHS
from mkl.features import compute

sys.stdout.reconfigure(encoding="utf-8")

t0 = time.time()
con = db.connect()
db.attach_parquet(con, "daily_channel", "episodes", "group_outages")

compute.build_all(con)
print(f"фичи канала построены [{time.time() - t0:.0f}s]", flush=True)
store.write(con, "feat_ext", "sensor")

compute.build_object_level(con)
store.write(con, "feat_object", "object")
compute.build_segment_level(con)
store.write(con, "feat_segment", "segment")

for name in ("feat_ext", "feat_object", "feat_segment"):
    r = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
    c = len(con.execute(f"DESCRIBE {name}").fetchall())
    print(f"{name:14} {r:>12,} строк  {c:>3} колонок")

print(f"\nвсего {time.time() - t0:.0f}s")
con.close()
