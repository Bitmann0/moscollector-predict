"""Проигрыватель событий в API (ML2-03, C5): поток дня и загрузка истории. Живое.

Источник — бандл C4 (data/interim/events_year=2026.parquet, читается через duckdb: он есть
в образе ml) или CSV в формате журнал_событий_пример.csv (только stdlib). Строки уходят
в POST /api/v1/ingest/events пачками EventRowIn с X-API-Key роли integration.

Поток дня. Темп задают времена событий: событие 00:10:00 приходит через 10 мин после начала
дня при --speed 1 и через 1 с при --speed 600. Пришедшее отправляется раз в --interval
секунд или сразу, как наберётся --batch-size строк. --align-to-clock начинает с текущего
времени суток МСК; более ранние события пропускаются, а с --catch-up уходят сразу, одной
серией пачек без уведомлений. --loop в конце дня (при --speed 1 и --align-to-clock —
в полночь МСК) удаляет события дня через DELETE /api/v1/ingest/day/{day} и начинает
с 00:00:00.

История (--bulk --from --to). Дни идут по порядку без темпа, пачками по 5 000 строк, так
быстро, как принимает api; после каждого дня — строка со счётчиками и скоростью. Нужна
журналу событий июня (решение D6) и динамике карточки прогноза: forecasts._dynamics берёт
события канала за 30 суток до asof. Прерванную загрузку можно запустить заново или с
другого --from: уже принятые строки api посчитает дублями.

История и догрузка идут с ?notify=false: api сохраняет и классифицирует события, но не
создаёт уведомлений — иначе прошлые тревоги пришли бы диспетчеру как новые. Поток — с
уведомлениями.

Пачку, не принятую из-за сети, 429 или 5xx, скрипт повторяет с паузами 1…30 с, после
восьмой попытки пропускает и идёт дальше. Повтор безопасен: api снимает дубли по полному
кортежу строки. Остальные 4xx не повторяются: та же пачка получит тот же ответ.

--latency-log пишет строку на пачку (CSV, при суффиксе .jsonl — JSONL): режим, времена
первого и последнего события, моменты отправки и ответа, счётчики партии и для потока —
задержку «событие → БД» для ML2-04. Задержка считается от момента, когда событие пришло
по темпу, до ответа api: api отвечает после коммита.

    python scripts/replay.py --day 2026-06-30 --loop --align-to-clock --catch-up
    python scripts/replay.py --bulk                      # 2026-05-02…2026-06-29
    python scripts/replay.py --bulk --from 2026-06-10    # продолжить прерванную загрузку
    python scripts/replay.py --speed 600      # сутки за 2,4 мин — только для видео
    python scripts/replay.py --source Materials/журнал_событий_пример.csv --day 2026-08-01 \\
        --speed 3600 --latency-log state/replay_latency.csv
    python scripts/replay.py --source bundle/data/interim/events_year=2026.parquet --dry-run

Пример CSV датирован 2026-08-01, позже demo_today: api посчитает его строки
outside_demo_window.
"""
import argparse
import bisect
import csv
import http.client
import json
import signal
import sys
import time as systime
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from _api import DEFAULT_BASE_URL, ROOT, Api, ApiError, env_value, safe_console

DEFAULT_DAY = date(2026, 6, 30)  # «сегодня» демо-стенда (решение D6)
# Окно истории: forecasts._dynamics для прогноза за 06-01 начинает с 05-03 (30 суток
# с asof включительно); 05-02 — запас в сутки. 30.06 проигрывается потоком.
HISTORY_FROM = date(2026, 5, 2)
HISTORY_TO = date(2026, 6, 29)
BATCH_LIMIT = 5000               # предел JSON-пачки по C5
LIVE_BATCH = 500                 # пачка потока по умолчанию; история идёт по BATCH_LIMIT
LIVE_PATH = "/ingest/events"
HISTORY_PATH = "/ingest/events?notify=false"
EVENTS_FILE = "data/interim/events_year=2026.parquet"
CSV_COLUMNS = ("ид_события", "ид_канала_данных", "дата", "время", "тревожное",
               "значение_датчика")
DAY_S = 86_400
MSK_OFFSET_S = 3 * 3600
MSK = timezone(timedelta(seconds=MSK_OFFSET_S), "MSK")  # как backend: без tzdata
BACKOFF_S = (1, 2, 4, 8, 16, 30, 30)  # паузы между восемью попытками: до 91 с на пачку
PROGRESS_EVERY_S = 60
# Ошибки одного вызова: HTTP-код, сеть и таймаут (OSError), оборванный ответ
# (HTTPException), не-JSON в ответе (ValueError).
CALL_ERRORS = (ApiError, OSError, ValueError, http.client.HTTPException)
# Столбцы журнала задержки (ML2-04). mode: live — поток, catch-up — догрузка, bulk — история.
LATENCY_FIELDS = ("mode", "pass", "rows", "event_first", "event_last", "sent_at", "done_at",
                  "latency_min_s", "latency_max_s", "request_s", "attempts", "status",
                  "accepted", "duplicates", "rejected", "outside_demo_window", "error")
# events_year=*.parquet пишет ml/src/mkl/ingest.py: build_events. ts = дата + время,
# alarm = lower(тревожное) IN ('t','true'); строки без времени отбрасываются: их не с чем
# сопоставить в темпе.
PARQUET_SQL = """
    SELECT event_id, ch, ts, alarm, val_raw FROM read_parquet(?)
    WHERE day = ? AND ts IS NOT NULL ORDER BY ts, event_id
"""


class SourceError(Exception):
    """Источник не читается: нет файла, колонок CSV или duckdb для parquet."""


@dataclass(frozen=True, slots=True)
class Event:
    offset: float  # секунды от полуночи дня
    row: dict      # тело EventRowIn с русскими именами полей (C5)


@dataclass
class Stats:
    passes: int = 0
    batches: int = 0
    failed: int = 0     # пачки, не принятые после всех попыток
    rows: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    outside: int = 0    # outside_demo_window
    resets: int = 0

    def minus(self, other: "Stats") -> "Stats":
        return Stats(**{f.name: getattr(self, f.name) - getattr(other, f.name)
                        for f in fields(self)})


# --- Источник ---

def seconds_of(text: str) -> float | None:
    """«HH:MM:SS» → секунды от полуночи; нечитаемое → None."""
    try:
        t = time.fromisoformat(text.strip())
    except (AttributeError, ValueError):
        return None
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def csv_event(record: dict) -> Event | None:
    """Строка CSV → событие; нечитаемая → None.

    Номера и время проверяются здесь: нечисло в ид_события даёт 422 на всю пачку.
    Пустое «тревожное» уходит как есть: api отклонит одну строку и посчитает её."""
    offset = seconds_of(record.get("время") or "")
    try:
        event_id, channel = int(record["ид_события"]), int(record["ид_канала_данных"])
    except (KeyError, TypeError, ValueError):
        return None
    if offset is None:
        return None
    return Event(offset, {"ид_события": event_id, "ид_канала_данных": channel,
                          "дата": record["дата"].strip(), "время": record["время"].strip(),
                          "тревожное": record.get("тревожное") or "",
                          "значение_датчика": record.get("значение_датчика")})


def load_csv(path: Path, days: Iterable[date]) -> dict[date, tuple[list[Event], int]]:
    """События дней из CSV за один проход файла: день → (события, нечитаемых строк)."""
    wanted = {day.isoformat(): day for day in days}
    events: dict[date, list[Event]] = {day: [] for day in wanted.values()}
    bad = dict.fromkeys(wanted.values(), 0)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = sorted(set(CSV_COLUMNS) - set(reader.fieldnames or ()))
        if missing:
            raise SourceError(f"{path}: нет колонок {', '.join(missing)}")
        for record in reader:
            day = wanted.get((record.get("дата") or "").strip())
            if day is None:
                continue
            event = csv_event(record)
            if event is None:
                bad[day] += 1
            else:
                events[day].append(event)
    return {day: (sorted(items, key=lambda e: e.offset), bad[day])
            for day, items in events.items()}


def parquet_event(event_id: int, channel: int, ts: datetime, alarm: bool | None,
                  value: str | None) -> Event:
    """Строка events_year=*.parquet → событие. alarm NULL (пустое «тревожное» в исходном
    журнале) уходит как false: null в «тревожное» api отклоняет всей пачкой (422)."""
    offset = ts.hour * 3600 + ts.minute * 60 + ts.second + ts.microsecond / 1e6
    # За 30.06 — 196 796 строк на 75 131 разную секунду: одинаковые строки храним по разу.
    return Event(offset, {"ид_события": event_id, "ид_канала_данных": channel,
                          "дата": sys.intern(ts.date().isoformat()),
                          "время": sys.intern(ts.strftime("%H:%M:%S")),
                          "тревожное": bool(alarm), "значение_датчика": value})


def load_parquet(path: Path, day: date) -> list[Event]:
    try:
        import duckdb
    except ImportError:
        raise SourceError("для parquet нужен duckdb (есть в образе ml; pip install duckdb) "
                          "или --source в формате журнал_событий_пример.csv") from None
    con = duckdb.connect()
    try:
        rows = con.execute(PARQUET_SQL, [str(path), day]).fetchall()
    finally:
        con.close()
    return [parquet_event(*row) for row in rows]


def load_days(path: Path, days: list[date]) -> Iterator[tuple[date, list[Event], int]]:
    """(день, события по возрастанию времени, нечитаемых строк CSV) по порядку days.
    Parquet читается запросом на день: в памяти одни сутки, а не вся история."""
    if not path.is_file():
        raise SourceError(f"нет файла {path}: бандл C4 раскладывает scripts/fetch_bundle.sh")
    if path.suffix.lower() == ".csv":
        by_day = load_csv(path, days)
        for day in days:
            yield day, *by_day[day]
    else:
        for day in days:
            yield day, load_parquet(path, day), 0


def load_events(path: Path, day: date) -> tuple[list[Event], int]:
    """События одного дня и число нечитаемых строк CSV."""
    _, events, bad = next(load_days(path, [day]))
    return events, bad


def default_source() -> Path:
    """Бандл в BUNDLE_DIR из окружения или .env (как в compose.real.yaml), иначе ./bundle."""
    return ROOT / (env_value("BUNDLE_DIR") or "bundle") / EVENTS_FILE


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


# --- Темп и пачки: чистые функции, часы передаются явно ---

def time_of_day(ts: float) -> float:
    """Секунды от полуночи МСК для времени Unix ts."""
    return (ts + MSK_OFFSET_S) % DAY_S


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, MSK).isoformat(timespec="milliseconds")


def split_at(events: list[Event], start: float) -> tuple[list[Event], list[Event]]:
    """(раньше start, не раньше start) — секунды от полуночи; events отсортированы."""
    cut = bisect.bisect_left(events, start, key=lambda e: e.offset)
    return events[:cut], events[cut:]


def due_times(events: list[Event], anchor: float, start: float, speed: float) -> list[float]:
    """Момент по часам, когда событие приходит: offset start соответствует моменту anchor."""
    return [anchor + (e.offset - start) / speed for e in events]


def next_flush(dues: list[float], i: int, last: float | None, interval: float,
               size: int) -> float:
    """Когда отправлять события с номера i. Первая отправка — с приходом события i,
    дальше — через interval после прошлой, но не раньше прихода события i; полная пачка
    из size событий уходит сразу, не дожидаясь interval."""
    if last is None:
        return dues[i]
    at = max(last + interval, dues[i])
    full = i + size - 1
    return min(at, dues[full]) if full < len(dues) else at


def chunks(lo: int, hi: int, size: int) -> list[tuple[int, int]]:
    """Границы пачек не больше size строк на отрезке [lo, hi)."""
    return [(a, min(a + size, hi)) for a in range(lo, hi, size)]


def transient(exc: Exception) -> bool:
    """Сеть, таймаут, 429 и 5xx проходят сами — повторяем; остальные 4xx — нет."""
    return not isinstance(exc, ApiError) or exc.status == 429 or exc.status >= 500


def call_with_retry(api, method: str, path: str, body, sleep,
                    backoff: tuple = BACKOFF_S) -> tuple[object, int, str | None]:
    """(тело ответа, число попыток, текст ошибки). Не бросает CALL_ERRORS."""
    attempts = 0
    while True:
        attempts += 1
        try:
            _, out = api.call(method, path, body)
            return out, attempts, None
        except CALL_ERRORS as exc:
            if not transient(exc) or attempts > len(backoff):
                return None, attempts, str(exc)
            sleep(backoff[attempts - 1])


# --- Журнал задержки ---

class LatencyLog:
    """Строка на пачку; файл дописывается, заголовок CSV — только в новый файл."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl = path.suffix.lower() == ".jsonl"
        fresh = not path.exists() or path.stat().st_size == 0
        self._file = path.open("a", encoding="utf-8", newline="")
        self._csv = None if self.jsonl else csv.DictWriter(self._file, LATENCY_FIELDS)
        if self._csv is not None and fresh:
            self._csv.writeheader()

    def write(self, record: dict) -> None:
        if self._csv is None:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            self._csv.writerow(record)
        self._file.flush()  # контейнер останавливают SIGTERM: записанное не должно теряться

    def close(self) -> None:
        self._file.close()


# --- Проигрыватель ---

class Replayer:
    """Проходы дня и загрузка истории. Часы (clock → время Unix) и sleep подменяются
    в тестах.

    Часы — time.time, а не monotonic: по ним выравнивается время суток МСК, и полночь
    второго прохода совпадает с полночью на часах стенда."""

    def __init__(self, api, *, speed: float = 1.0, interval: float = 5.0,
                 batch_size: int = LIVE_BATCH, history_size: int = BATCH_LIMIT,
                 clock=systime.time, sleep=systime.sleep, log: LatencyLog | None = None,
                 out=print) -> None:
        self.api = api
        self.speed = speed
        self.interval = interval
        self.batch_size = batch_size
        self.history_size = history_size
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.out = out
        self.stats = Stats()
        self._printed = clock()
        self._window_latency = 0.0  # наибольшая задержка с прошлой строки прогресса

    def wait_until(self, moment: float) -> float:
        """Спит до moment; возвращает текущее время, но не меньше moment."""
        now = self.clock()
        if now < moment:
            self.sleep(moment - now)
            now = self.clock()
        return max(now, moment)

    def send(self, events: list[Event], lo: int, hi: int, *, mode: str = "live",
             pass_no: int = 0, dues: list[float] | None = None) -> None:
        """Пачка events[lo:hi]. Поток (dues задан) — с уведомлениями и задержкой в журнале,
        история и догрузка — с notify=false."""
        rows = [e.row for e in events[lo:hi]]
        path = LIVE_PATH if mode == "live" else HISTORY_PATH
        sent = self.clock()
        out, attempts, error = call_with_retry(self.api, "POST", path, rows, self.sleep)
        done = self.clock()
        out = out if isinstance(out, dict) else {}
        s = self.stats
        s.batches += 1
        s.rows += len(rows)
        first, last = rows[0], rows[-1]
        record = {"mode": mode, "pass": pass_no, "rows": len(rows),
                  "event_first": f"{first['дата']}T{first['время']}",
                  "event_last": f"{last['дата']}T{last['время']}",
                  "sent_at": stamp(sent), "done_at": stamp(done),
                  "latency_min_s": None, "latency_max_s": None,
                  "request_s": round(done - sent, 3), "attempts": attempts,
                  "status": "error", "accepted": 0, "duplicates": 0, "rejected": 0,
                  "outside_demo_window": 0, "error": error or ""}
        if error:
            s.failed += 1
            self.out(f"{stamp(done)}  пачка {len(rows)} строк {record['event_first']}…"
                     f"{record['event_last']} не принята за {attempts} попыток: {error}")
        else:
            record.update(status=out.get("status", ""),
                          **{key: out.get(key, 0) for key in
                             ("accepted", "duplicates", "rejected", "outside_demo_window")})
            s.accepted += record["accepted"]
            s.duplicates += record["duplicates"]
            s.rejected += record["rejected"]
            s.outside += record["outside_demo_window"]
            if dues is not None:
                latency = max(0.0, done - dues[lo])
                self._window_latency = max(self._window_latency, latency)
                record.update(latency_min_s=round(max(0.0, done - dues[hi - 1]), 3),
                              latency_max_s=round(latency, 3))
        if self.log is not None:
            self.log.write(record)

    def history(self, events: list[Event], mode: str, pass_no: int = 0) -> None:
        """Без темпа, пачками по history_size, без уведомлений."""
        for lo, hi in chunks(0, len(events), self.history_size):
            self.send(events, lo, hi, mode=mode, pass_no=pass_no)

    def progress(self, event: Event | None, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._printed < PROGRESS_EVERY_S:
            return
        s = self.stats
        where = f"событие {event.row['время']}" if event else "событий нет"
        self.out(f"{stamp(now)[11:19]}  {where}: строк {s.rows}, принято {s.accepted}, "
                 f"дублей {s.duplicates}, отклонено {s.rejected}, вне окна {s.outside}, "
                 f"пачек с ошибкой {s.failed}, задержка до {self._window_latency:.1f} с")
        self._printed, self._window_latency = now, 0.0

    def play(self, events: list[Event], anchor: float, start: float, pass_no: int) -> None:
        """Один проход: events уже без пропущенных, моменту anchor соответствует start."""
        dues = due_times(events, anchor, start, self.speed)
        i, last = 0, None
        while i < len(dues):
            now = self.wait_until(next_flush(dues, i, last, self.interval, self.batch_size))
            j = bisect.bisect_right(dues, now)
            for lo, hi in chunks(i, j, self.batch_size):
                self.send(events, lo, hi, pass_no=pass_no, dues=dues)
            i, last = j, now
            self.progress(events[j - 1])
        self.progress(events[-1] if events else None, force=True)

    def reset_day(self, day: date) -> None:
        out, attempts, error = call_with_retry(self.api, "DELETE", f"/ingest/day/{day}", None,
                                               self.sleep)
        self.stats.resets += 1
        now = stamp(self.clock())
        if error:
            # Проход всё равно начинается: повторные строки api посчитает дублями.
            self.out(f"{now}  DELETE /ingest/day/{day} не прошёл за {attempts} попыток: {error}")
        else:
            deleted = out.get("deleted_events") if isinstance(out, dict) else None
            self.out(f"{now}  DELETE /ingest/day/{day}: удалено событий {deleted}")

    def run(self, events: list[Event], day: date, *, align: bool = False, loop: bool = False,
            catch_up: bool = False, max_passes: int | None = None) -> Stats:
        """Проходы дня; при loop — бесконечно, max_passes ограничивает их в тестах.
        catch_up — перед первым выровненным проходом отправить события 00:00…сейчас."""
        anchor = self.clock()
        start = time_of_day(anchor) if align else 0.0
        while True:
            self.stats.passes += 1
            past, todo = split_at(events, start)
            if catch_up and past:
                t0 = self.clock()
                self.out(f"догрузка {day} 00:00:00…{hhmmss(start)}: {len(past)} строк "
                         f"без уведомлений")
                self.history(past, "catch-up", self.stats.passes)
                self.out(f"догрузка закончена за {self.clock() - t0:.0f} с")
                catch_up = False
            self.out(f"проход {self.stats.passes}: {day} с {hhmmss(start)}, событий "
                     f"{len(todo)} из {len(events)}, темп x{self.speed:g}")
            self.play(todo, anchor, start, self.stats.passes)
            if not loop or (max_passes is not None and self.stats.passes >= max_passes):
                return self.stats
            midnight = anchor + (DAY_S - start) / self.speed
            self.wait_until(midnight)
            self.reset_day(day)
            anchor, start = midnight, 0.0

    def bulk(self, days: Iterable[tuple[date, list[Event], int]]) -> Stats:
        """История по дням без темпа; после каждого дня — строка со счётчиками и скоростью."""
        for day, events, bad in days:
            before, t0 = replace(self.stats), self.clock()
            self.history(events, "bulk")
            d, spent = self.stats.minus(before), self.clock() - t0
            self.out(f"{day}: строк {d.rows}, принято {d.accepted}, дублей {d.duplicates}, "
                     f"отклонено {d.rejected}, пачек с ошибкой {d.failed}"
                     + (f", нечитаемых строк CSV {bad}" if bad else "")
                     + f"; {spent:.1f} с, {rate(d.rows, spent)} строк/с")
        return self.stats


def rate(rows: int, seconds: float) -> str:
    return f"{rows / seconds:.0f}" if seconds > 0 else "—"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Проигрыватель событий в API (ML2-03).")
    parser.add_argument("--day", type=date.fromisoformat, default=DEFAULT_DAY,
                        help=f"день потока (по умолчанию {DEFAULT_DAY})")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="множитель времени: 1 — реальный темп, 600 — сутки за 2,4 мин")
    parser.add_argument("--loop", action="store_true",
                        help="после конца дня удалить его события и начать заново")
    parser.add_argument("--align-to-clock", action="store_true",
                        help="начать с текущего времени суток МСК")
    parser.add_argument("--catch-up", action="store_true",
                        help="с --align-to-clock: сначала отправить события дня 00:00…сейчас, "
                             "без уведомлений")
    parser.add_argument("--bulk", action="store_true",
                        help="загрузить историю --from…--to без темпа и без уведомлений")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat,
                        default=HISTORY_FROM, help=f"первый день истории (по умолчанию "
                                                   f"{HISTORY_FROM})")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, default=HISTORY_TO,
                        help=f"последний день истории (по умолчанию {HISTORY_TO})")
    parser.add_argument("--base-url", "--api", dest="base_url", default=DEFAULT_BASE_URL,
                        help=f"адрес api (по умолчанию {DEFAULT_BASE_URL})")
    parser.add_argument("--api-key",
                        help="X-API-Key роли integration (по умолчанию INTEGRATION_API_KEY "
                             "из окружения или .env)")
    parser.add_argument("--source", type=Path,
                        help=f"parquet бандла C4 или CSV как журнал_событий_пример.csv "
                             f"(по умолчанию <BUNDLE_DIR или bundle>/{EVENTS_FILE})")
    parser.add_argument("--batch-size", type=int,
                        help=f"строк в пачке, не больше {BATCH_LIMIT}; по умолчанию "
                             f"{LIVE_BATCH} в потоке и {BATCH_LIMIT} в истории и догрузке")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="период отправки пришедших событий, с; полная пачка уходит сразу")
    parser.add_argument("--latency-log", type=Path,
                        help="журнал задержки «событие → БД»: .csv или .jsonl")
    parser.add_argument("--dry-run", action="store_true",
                        help="прочитать источник и напечатать план, ничего не отправлять")
    return parser


def check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.batch_size is not None and not 1 <= args.batch_size <= BATCH_LIMIT:
        parser.error(f"--batch-size от 1 до {BATCH_LIMIT}")
    if args.speed <= 0 or args.interval <= 0:
        parser.error("--speed и --interval должны быть больше нуля")
    if args.catch_up and not args.align_to_clock:
        parser.error("--catch-up работает только с --align-to-clock")
    if args.bulk and (args.loop or args.align_to_clock or args.catch_up):
        parser.error("--bulk не сочетается с --loop, --align-to-clock и --catch-up")
    if args.bulk and args.date_from > args.date_to:
        parser.error("--from позже --to")


def main(argv: list[str] | None = None) -> int:
    safe_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    check_args(parser, args)
    source = args.source or default_source()
    history_size = args.batch_size or BATCH_LIMIT

    if args.bulk:
        days = days_between(args.date_from, args.date_to)
        print(f"история {args.date_from}…{args.date_to} ({len(days)} дн.) из {source}: POST "
              f"{args.base_url}/api/v1{HISTORY_PATH} пачками до {history_size} строк")
        if not source.is_file():
            print(f"нет файла {source}", file=sys.stderr)
            return 1
        if args.dry_run:
            return 0
    else:
        try:
            events, bad = load_events(source, args.day)
        except SourceError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"источник {source}: событий за {args.day} — {len(events)}"
              + (f", нечитаемых строк пропущено {bad}" if bad else ""))
        if not events:
            print(f"за {args.day} в источнике нет событий", file=sys.stderr)
            return 1
        print(f"события {hhmmss(events[0].offset)}…{hhmmss(events[-1].offset)}; POST "
              f"{args.base_url}/api/v1{LIVE_PATH}: пачки до {args.batch_size or LIVE_BATCH} "
              f"строк раз в {args.interval:g} с, темп x{args.speed:g}"
              + (", старт с текущего времени МСК" if args.align_to_clock else "")
              + (", сначала догрузка 00:00…сейчас" if args.catch_up else "")
              + (", по кругу со сбросом дня" if args.loop else ""))
        if args.dry_run:
            return 0

    api_key = args.api_key or env_value("INTEGRATION_API_KEY")
    if not api_key:
        print("INTEGRATION_API_KEY не задан ни в --api-key, ни в окружении, ни в .env",
              file=sys.stderr)
        return 1
    log = LatencyLog(args.latency_log) if args.latency_log else None
    replayer = Replayer(Api(args.base_url, api_key=api_key), speed=args.speed,
                        interval=args.interval, batch_size=args.batch_size or LIVE_BATCH,
                        history_size=history_size, log=log,
                        out=lambda text: print(text, flush=True))
    # docker stop шлёт SIGTERM: печатаем итог, как при Ctrl+C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    started, code = systime.time(), 0
    try:
        if args.bulk:
            replayer.bulk(load_days(source, days))
        else:
            replayer.run(events, args.day, align=args.align_to_clock, loop=args.loop,
                         catch_up=args.catch_up)
    except KeyboardInterrupt:
        print("остановлено", flush=True)
    except SourceError as exc:
        print(exc, file=sys.stderr)
        code = 1
    finally:
        if log is not None:
            log.close()
    s, spent = replayer.stats, systime.time() - started
    print(f"итого за {spent:.0f} с ({rate(s.rows, spent)} строк/с): проходов {s.passes}, "
          f"пачек {s.batches} (с ошибкой {s.failed}), строк {s.rows}: принято {s.accepted}, "
          f"дублей {s.duplicates}, отклонено {s.rejected}, вне окна {s.outside}; "
          f"сбросов дня {s.resets}")
    return 1 if s.failed else code


if __name__ == "__main__":
    sys.exit(main())
