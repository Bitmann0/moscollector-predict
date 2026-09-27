"""scripts/replay.py (ML2-03): темп, пачки, выравнивание по МСК, сброс дня, журнал задержки,
догрузка дня и загрузка истории без уведомлений.

Без сети и без настоящего сна: часы и sleep подменяются, api — фейк или TestClient.
"""
import csv
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import replay  # scripts/ — не пакет: скрипты запускаются файлами
from _api import ApiError

DAY = date(2026, 6, 30)


def at(hour: int, day: date = DAY) -> float:
    """Время Unix для day hour:00:00 МСК."""
    return datetime.combine(day, time(hour), replay.MSK).timestamp()


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeApi:
    """Отвечает как api; каждый вызов занимает latency секунд по фейковым часам.
    errors — ошибки, которые бросают первые вызовы POST по порядку."""

    def __init__(self, clock: FakeClock, latency: float = 0.0, errors=()) -> None:
        self.clock = clock
        self.latency = latency
        self.errors = list(errors)
        self.calls: list[tuple[float, str, str, object]] = []

    def call(self, method, path, body=None, *, timeout=None):
        self.calls.append((self.clock.now, method, path, body))
        self.clock.now += self.latency
        if method == "DELETE":
            return 200, {"day": path.rsplit("/", 1)[1], "deleted_events": 7}
        if self.errors:
            raise self.errors.pop(0)
        return 201, {"batch_id": len(self.calls), "kind": "smvu", "rows_total": len(body),
                     "accepted": len(body) - 1, "duplicates": 1, "rejected": 0,
                     "outside_demo_window": 0, "status": "accepted"}

    def posts(self) -> list[list[dict]]:
        return [body for _, method, _, body in self.calls if method == "POST"]


def event(offset: int, event_id: int = 1, channel: int = 9000001, *, day: date = DAY,
          alarm: bool = False, value: str = "1.0") -> replay.Event:
    ts = datetime.combine(day, time()) + timedelta(seconds=offset)
    return replay.parquet_event(event_id, channel, ts, alarm, value)


def replayer(clock: FakeClock, api: FakeApi, **kwargs) -> replay.Replayer:
    return replay.Replayer(api, clock=clock, sleep=clock.sleep, out=lambda _: None, **kwargs)


def test_batches_never_exceed_limit():
    clock = FakeClock(at(0))
    api = FakeApi(clock)
    events = [event(0, n) for n in range(12_001)]
    replayer(clock, api, batch_size=replay.BATCH_LIMIT).run(events, DAY)
    assert [len(body) for body in api.posts()] == [5000, 5000, 2001]
    assert replay.chunks(0, 7, 3) == [(0, 3), (3, 6), (6, 7)]


def test_batch_size_over_limit_is_rejected_by_cli():
    with pytest.raises(SystemExit):
        replay.main(["--batch-size", str(replay.BATCH_LIMIT + 1), "--dry-run"])


def test_full_batch_is_sent_before_interval():
    dues = [0.0, 1.0, 2.0, 3.0]
    assert replay.next_flush(dues, 0, None, 100, 2) == 0.0      # первая — с приходом
    assert replay.next_flush(dues, 1, 0.0, 100, 2) == 2.0       # пачка из 2 набралась к 2 с
    assert replay.next_flush(dues, 3, 2.0, 100, 2) == 102.0     # хвост ждёт interval
    assert replay.next_flush(dues, 3, 2.0, 0.5, 2) == 3.0       # но не раньше прихода


def test_align_to_clock_skips_earlier_events():
    clock = FakeClock(at(14))
    api = FakeApi(clock)
    events = [event(13 * 3600 + 3599, 1), event(14 * 3600, 2), event(14 * 3600 + 10, 3)]
    stats = replayer(clock, api, interval=1).run(events, DAY, align=True)
    assert [row["время"] for body in api.posts() for row in body] == ["14:00:00", "14:00:10"]
    assert [t - at(14) for t, *_ in api.calls] == [0, 10]
    assert stats.rows == 2


@pytest.mark.parametrize("speed, waits", [(1, [600, 600]), (600, [1, 1])])
def test_speed_scales_waits(speed, waits):
    clock = FakeClock(at(0))
    api = FakeApi(clock)
    events = [event(0, 1), event(600, 2), event(1200, 3)]
    replayer(clock, api, speed=speed, interval=0.5).run(events, DAY)
    assert clock.sleeps == pytest.approx(waits)
    assert len(api.posts()) == 3


def test_loop_resets_day_at_midnight_and_starts_over():
    clock = FakeClock(at(10))
    api = FakeApi(clock, latency=0.25)
    events = [event(0, 1), event(12 * 3600, 2), event(86_399, 3)]
    stats = replayer(clock, api).run(events, DAY, align=True, loop=True, max_passes=2)
    methods = [method for _, method, _, _ in api.calls]
    assert methods == ["POST", "POST", "DELETE", "POST", "POST", "POST"]
    delete_at, _, delete_path, _ = api.calls[2]
    assert delete_path == "/ingest/day/2026-06-30"
    assert delete_at == pytest.approx(at(0, date(2026, 7, 1)))  # полночь МСК
    assert api.calls[3][3][0]["время"] == "00:00:00"  # второй проход — с начала дня
    assert (stats.passes, stats.resets, stats.rows) == (2, 1, 5)


@pytest.mark.parametrize("name", ["latency.csv", "latency.jsonl"])
def test_latency_log_row_per_batch(tmp_path, name):
    clock = FakeClock(at(0))
    api = FakeApi(clock, latency=0.25, errors=[ApiError("POST", "/ingest/events", 422, "bad")])
    path = tmp_path / "state" / name
    log = replay.LatencyLog(path)
    events = [event(0, 1), event(0, 2), event(3, 3), event(4, 4)]
    replayer(clock, api, interval=5, log=log).run(events, DAY)
    log.close()
    if name.endswith(".jsonl"):
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    else:
        with path.open(encoding="utf-8", newline="") as fh:
            records = list(csv.DictReader(fh))
    assert len(records) == 2
    failed, ok = records
    assert failed["status"] == "error" and "HTTP 422" in failed["error"]
    assert str(failed["latency_max_s"] or "") == ""
    assert (ok["event_first"], ok["event_last"]) == ("2026-06-30T00:00:03",
                                                     "2026-06-30T00:00:04")
    # Прошлая отправка — в 00:00:00, interval 5 с: пачка уходит в 00:00:05, ответ через
    # 0,25 с. Событие 00:00:03 ждало 2,25 с, 00:00:04 — 1,25 с.
    assert float(ok["latency_max_s"]) == pytest.approx(2.25)
    assert float(ok["latency_min_s"]) == pytest.approx(1.25)
    assert (int(ok["accepted"]), int(ok["duplicates"]), int(ok["rows"])) == (1, 1, 2)
    assert ok["sent_at"] == "2026-06-30T00:00:05.000+03:00"


def test_transient_errors_are_retried_with_backoff():
    clock = FakeClock(at(0))
    api = FakeApi(clock, errors=[ApiError("POST", "/ingest/events", 503, ""),
                                 ConnectionRefusedError("down")])
    stats = replayer(clock, api).run([event(0)], DAY)
    assert clock.sleeps == [1, 2]
    assert (stats.batches, stats.failed, len(api.posts())) == (1, 0, 3)


def test_failed_batch_does_not_stop_the_pass():
    clock = FakeClock(at(0))
    down = [OSError("timeout")] * (len(replay.BACKOFF_S) + 1)
    api = FakeApi(clock, errors=down)
    stats = replayer(clock, api, interval=1).run([event(0, 1), event(5, 2)], DAY)
    assert (stats.batches, stats.failed, stats.rows) == (2, 1, 2)
    assert clock.sleeps[:len(replay.BACKOFF_S)] == list(replay.BACKOFF_S)


def test_csv_rows_follow_sample_format(tmp_path):
    path = tmp_path / "journal.csv"
    path.write_text(
        '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"\n'
        '11,9000001,"2026-06-30","10:00:05",false,"28"\n'
        '10,9000001,"2026-06-30","09:00:00",t,"Обнаружен газ"\n'
        '12,9000001,"2026-08-01","10:00:00",false,"28"\n'
        '13,9000001,"2026-06-30","25:00:00",false,"28"\n'
        'x,9000001,"2026-06-30","10:00:00",false,"28"\n', encoding="utf-8-sig")
    events, bad = replay.load_events(path, DAY)
    assert bad == 2  # 25:00:00 и нечисловой ид_события; чужой день не считается
    assert [e.offset for e in events] == [9 * 3600, 10 * 3600 + 5]
    assert events[0].row == {"ид_события": 10, "ид_канала_данных": 9000001,
                             "дата": "2026-06-30", "время": "09:00:00", "тревожное": "t",
                             "значение_датчика": "Обнаружен газ"}


def test_csv_without_required_columns_is_source_error(tmp_path):
    path = tmp_path / "journal.csv"
    path.write_text("ид_события,дата\n1,2026-06-30\n", encoding="utf-8")
    with pytest.raises(replay.SourceError, match="время"):
        replay.load_events(path, DAY)


def test_parquet_row_maps_to_event_row():
    ts = datetime.combine(DAY, time(10, 1, 2))  # duckdb отдаёт TIMESTAMP без пояса
    item = replay.parquet_event(4474543298, 288201, ts, None, None)
    assert item.offset == 10 * 3600 + 62
    assert item.row == {"ид_события": 4474543298, "ид_канала_данных": 288201,
                        "дата": "2026-06-30", "время": "10:01:02", "тревожное": False,
                        "значение_датчика": None}


def test_catch_up_sends_past_events_silently_then_paces_the_rest():
    clock = FakeClock(at(14))
    api = FakeApi(clock)
    events = [event(10 * 3600, 1), event(13 * 3600 + 3599, 2), event(14 * 3600, 3),
              event(14 * 3600 + 10, 4)]
    stats = replayer(clock, api, interval=1).run(events, DAY, align=True, catch_up=True)
    sent = [(t - at(14), path, [row["время"] for row in body]) for t, _, path, body in api.calls]
    assert sent == [(0, replay.HISTORY_PATH, ["10:00:00", "13:59:59"]),
                    (0, replay.LIVE_PATH, ["14:00:00"]),
                    (10, replay.LIVE_PATH, ["14:00:10"])]
    assert stats.rows == 4


def test_catch_up_needs_align_to_clock():
    with pytest.raises(SystemExit):
        replay.main(["--catch-up", "--dry-run"])
    with pytest.raises(SystemExit):
        replay.main(["--bulk", "--loop", "--dry-run"])


def test_bulk_sends_days_without_pacing_or_notifications():
    clock = FakeClock(at(12))
    api = FakeApi(clock, latency=0.5)
    lines = []
    may = date(2026, 5, 2)
    days = [(may, [event(n, n, day=may) for n in range(7_001)], 0),
            (date(2026, 5, 3), [], 0),
            (date(2026, 5, 4), [event(86_399, 1, day=date(2026, 5, 4))], 3)]
    stats = replay.Replayer(api, clock=clock, sleep=clock.sleep, out=lines.append).bulk(days)
    assert [(path, len(body)) for _, _, path, body in api.calls] == [
        (replay.HISTORY_PATH, 5000), (replay.HISTORY_PATH, 2001), (replay.HISTORY_PATH, 1)]
    assert clock.sleeps == []
    assert (stats.rows, stats.accepted, stats.duplicates) == (7002, 6999, 3)
    assert lines[0].startswith("2026-05-02: строк 7001, принято 6999, дублей 2")
    assert lines[0].endswith("1.0 с, 7001 строк/с")
    assert "нечитаемых строк CSV 3" in lines[2]


def test_csv_days_are_read_in_one_pass(tmp_path):
    path = tmp_path / "journal.csv"
    lines = ["ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика",
             "1,9000001,2026-05-02,10:00:00,f,1",
             "2,9000001,2026-05-03,09:00:00,f,1",
             "3,9000001,2026-05-04,09:00:00,f,1"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    days = replay.days_between(date(2026, 5, 2), date(2026, 5, 3))
    got = [(day, [e.row["ид_события"] for e in events], bad)
           for day, events, bad in replay.load_days(path, days)]
    assert got == [(date(2026, 5, 2), [1], 0), (date(2026, 5, 3), [2], 0)]


def test_history_window_covers_dynamics_of_first_forecast():
    from app.services.forecasts import DYNAMICS_DAYS

    first_asof = date(2026, 6, 1)
    assert replay.HISTORY_FROM <= first_asof - timedelta(days=DYNAMICS_DAYS - 1)
    assert replay.HISTORY_TO == DAY - timedelta(days=1)


class ClientApi:
    """Те же вызовы, что шлёт scripts/_api.Api, но в приложение в памяти."""

    def __init__(self, client) -> None:
        self.client = client

    def call(self, method, path, body=None, *, timeout=None):
        resp = self.client.request(method, "/api/v1" + path, json=body)
        if resp.status_code >= 400:
            raise ApiError(method, path, resp.status_code, resp.text)
        return resp.status_code, resp.json()


def test_rows_are_accepted_by_ingest_and_reset_by_loop(integration, admin):
    clock = FakeClock(at(0))
    events = [event(36_000 + n, 100 + n, 9000001 + n % 3) for n in range(6)]
    runner = replay.Replayer(ClientApi(integration), speed=3600, interval=1, batch_size=4,
                             clock=clock, sleep=clock.sleep, out=lambda _: None)
    stats = runner.run(events, DAY, loop=True, max_passes=2)
    assert (stats.accepted, stats.duplicates, stats.rejected, stats.failed) == (12, 0, 0, 0)
    assert stats.resets == 1
    listed = admin.get("/api/v1/events", params={"from": "2026-06-30", "to": "2026-06-30"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 6


def test_history_is_silent_and_live_notifies(integration, db):
    from app import models
    from sqlalchemy import func, select

    clock = FakeClock(at(0))
    runner = replay.Replayer(ClientApi(integration), clock=clock, sleep=clock.sleep,
                             out=lambda _: None)
    alarm = {"alarm": True, "value": "Обнаружен газ"}
    may = date(2026, 5, 20)
    runner.bulk([(may, [event(36_000, 1, 9000004, day=may, **alarm)], 0)])
    runner.run([event(36_000, 2, 9000004, **alarm)], DAY)
    classes = db.scalars(select(models.Event.event_class)).all()
    notified = db.scalar(select(func.count()).select_from(models.Notification))
    assert (classes, notified) == (["alarm", "alarm"], 1)
