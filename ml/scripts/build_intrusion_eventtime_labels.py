"""Rebuild versioned C alarm labels when event logs and object days grow."""
import argparse
import datetime as dt
import json
import sys

from mkl import db
from mkl.config import PATHS
from mkl.intrusion_target import build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=dt.date.fromisoformat,
                        default=dt.date(2023, 1, 1))
    parser.add_argument("--end", type=dt.date.fromisoformat,
                        help="Last forecast day; defaults to penultimate observed day")
    args = parser.parse_args()
    con = db.connect("8GB", 4)
    db.attach_events(con)
    path = (PATHS.features / "object.parquet").as_posix().replace("'", "''")
    con.execute(f"CREATE TEMP VIEW object_days AS SELECT obj, day FROM read_parquet('{path}')")
    last = con.execute("SELECT max(day) FROM object_days").fetchone()[0]
    end = args.end or last - dt.timedelta(days=1)
    if end >= last:
        raise ValueError("last forecast day must precede final observed day")
    build(con, args.start, end)
    labels = PATHS.features / "label_intrusion_eventtime_v2.parquet"
    events = PATHS.features / "intrusion_eventtime_days_v2.parquet"
    con.execute(f"COPY label_intrusion_eventtime TO '{labels.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute(f"COPY intrusion_eventtime_days TO '{events.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    result = {"version": 2, "start": str(args.start), "end": str(end),
              "candidate_rows": con.execute("SELECT count(*) FROM label_intrusion_eventtime").fetchone()[0],
              "known_rows": con.execute("SELECT count(*) FROM label_intrusion_eventtime WHERE known").fetchone()[0],
              "positive_rows": con.execute("SELECT count(*) FROM label_intrusion_eventtime WHERE y=1").fetchone()[0],
              "event_days": con.execute("SELECT count(*) FROM intrusion_eventtime_days WHERE positive").fetchone()[0]}
    (PATHS.reports / "intrusion_eventtime_v2_build.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    con.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
