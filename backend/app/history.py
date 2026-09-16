"""Read-only access to the independently prepared annual feature export."""

from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import duckdb


@contextmanager
def history_connection(path: Path | None):
    if path is None or not path.is_file():
        raise ValueError("Годовая история не подключена")
    with duckdb.connect() as con:
        con.execute("SET memory_limit='512MB'")
        con.execute("SET threads=2")
        con.execute("SET TimeZone='UTC'")
        con.read_parquet(str(path)).create_view("history")
        yield con


def records(query):
    columns = [c[0] for c in query.description]
    return [dict(zip(columns, row, strict=True)) for row in query.fetchall()]


def history_summary(path: Path | None) -> dict:
    with history_connection(path) as con:
        result = records(
            con.execute("""SELECT count(*) observed_channel_days,
            count(DISTINCT channel_id) channels, min(local_date) date_from,
            max(local_date) date_to FROM history""")
        )[0]
    return {**result, "mode": "historical", "prediction_available": False}


DAY_FIELDS = """channel_id, sensor_type, local_date, event_count, alarm_count,
    alarm_fraction, median_events_previous_30d, activity_ratio_to_past,
    observed_days_previous_30d, days_since_previous, baseline_available,
    feature_available_at"""


def history_day(path: Path | None, day: date, channel: int | None, limit: int, offset: int) -> dict:
    with history_connection(path) as con:
        where = "local_date = ?"
        params = [day]
        if channel is not None:
            where += " AND channel_id = ?"
            params.append(channel)
        total = con.execute(f"SELECT count(*) FROM history WHERE {where}", params).fetchone()[0]
        items = records(
            con.execute(
                f"SELECT {DAY_FIELDS} FROM history WHERE {where} "
                "ORDER BY baseline_available DESC, activity_ratio_to_past DESC NULLS LAST, channel_id "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        )
    return {"day": day, "total": total, "items": items, "prediction_available": False}


def history_channel(path: Path | None, channel: int, end: date, days: int) -> dict:
    start = end - timedelta(days=days - 1)
    with history_connection(path) as con:
        items = records(
            con.execute(
                f"SELECT {DAY_FIELDS} FROM history WHERE channel_id=? AND local_date BETWEEN ? AND ? "
                "ORDER BY local_date",
                [channel, start, end],
            )
        )
    return {
        "channel_id": channel,
        "date_from": start,
        "date_to": end,
        "observed_days": len(items),
        "unobserved_days": days - len(items),
        "items": items,
    }
