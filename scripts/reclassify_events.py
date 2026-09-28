"""Пересчёт класса, подсказки и группы аварии у загруженных событий СМВУ. Живое.

Правила C5 поменялись 28.09 по ответам заказчика (analysis/qa_customer_2026-09-28.md):
группы аварий, «тревожное сообщение», подсказки «вероятно, ППР или ТО». Приём ставит
класс один раз, поэтому события, загруженные раньше, хранят старые класс и подсказку,
а группы у них нет. Скрипт пересчитывает все три поля теми же функциями, что и приём:
classify и series_hints из backend/app/services/semantics.py.

Почему напрямую через БД, а не через admin-эндпоинт:
- на стенде работает образ без этой ветки: эндпоинта в нём нет, а контейнеры стенда
  не перезапускаем;
- история стенда — 10 428 318 строк; через HTTP её пришлось бы вычитать и отправить
  обратно, а приём шёл 3 829 строк в секунду (docs/submission/08-performance.md) —
  по этой скорости 45 минут одной записи. Скрипт читает строки и пишет только
  изменившиеся;
- один запрос на эндпоинт держал бы HTTP-соединение все минуты пересчёта.

Как. По суткам МСК: события [D − 10 мин, D + 1 + 10 мин] читаются одним запросом,
справочник каналов — один раз на весь прогон. Запаса в 10 минут хватает, чтобы
подсказка серии на стыке суток была той же, что при приёме: окно серии — 10 минут.
UPDATE — только у строк суток D, где изменились класс, подсказка или группа, и одна
транзакция на сутки: прерванный прогон можно повторить, пересчитанные сутки дадут
0 изменений.

Колонка. На БД без миграции 0002 (стенд на образе до этой ветки) скрипт сам добавляет
events.incident_group и частичный индекс, не трогая alembic_version: старый образ при
перезапуске выполняет alembic upgrade head и не стартовал бы, найдя в БД неизвестную
ему ревизию. Миграция 0002 существующие колонку и индекс пропускает.

    python scripts/reclassify_events.py                 # DATABASE_URL из окружения или .env
    python scripts/reclassify_events.py --from 2026-06-01 --to 2026-06-30 --dry-run
"""
import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from _api import ROOT, env_value, safe_console
from sqlalchemy import Engine, bindparam, create_engine, func, inspect, select, text

sys.path.insert(0, str(Path(ROOT) / "backend"))  # backend/ — не установленный пакет
from app import models
from app.services import semantics
from app.services.helpers import MSK, from_db, to_db

INDEX = "ix_events_incident_group"
EVENTS = models.Event.__table__
MARGIN = semantics.SERIES_WINDOW


@dataclass
class Totals:
    rows: int = 0
    changed: int = 0                                     # строк, где изменилось хоть что-то
    classes: Counter = field(default_factory=Counter)    # (было, стало), если класс сменился
    hints_changed: int = 0
    groups_changed: int = 0
    groups: Counter = field(default_factory=Counter)     # группа после пересчёта, все строки
    hints: Counter = field(default_factory=Counter)      # вид подсказки после пересчёта

    def add(self, other: "Totals") -> None:
        self.rows += other.rows
        self.changed += other.changed
        self.classes.update(other.classes)
        self.hints_changed += other.hints_changed
        self.groups_changed += other.groups_changed
        self.groups.update(other.groups)
        self.hints.update(other.hints)


def ensure_column(engine: Engine) -> bool:
    """True, если колонку или индекс пришлось добавить."""
    insp = inspect(engine)
    added = False
    with engine.begin() as conn:
        if "incident_group" not in {c["name"] for c in insp.get_columns("events")}:
            conn.execute(text("ALTER TABLE events ADD COLUMN incident_group VARCHAR(32)"))
            added = True
        if INDEX not in {i["name"] for i in insp.get_indexes("events")}:
            conn.execute(text(f"CREATE INDEX {INDEX} ON events (incident_group) "
                              "WHERE incident_group IS NOT NULL"))
            added = True
    return added


def has_column(engine: Engine) -> bool:
    return "incident_group" in {c["name"] for c in inspect(engine).get_columns("events")}


def channel_map(conn) -> dict[int, tuple[str | None, str | None, str | None]]:
    rows = conn.execute(select(models.RefChannel.id, models.RefChannel.sensor_type,
                               models.RefChannel.obj_id, models.RefObject.parent_id)
                        .outerjoin(models.RefObject,
                                   models.RefObject.id == models.RefChannel.obj_id))
    return {cid: (st, obj, parent) for cid, st, obj, parent in rows}


def hint_kind(hint: str | None) -> str:
    if hint is None:
        return "нет"
    if hint.startswith(semantics.PPR_HINT + ": серия"):
        return "ППР/ТО, серия"
    if hint.startswith(semantics.PPR_HINT):
        return "ППР/ТО, газ в рабочие часы"
    return hint


def reclassify_day(conn, day: date, channels: dict, *, group_column: bool,
                   write: bool) -> Totals:
    start = datetime.combine(day, datetime.min.time(), MSK)
    end = start + timedelta(days=1)
    group_col = EVENTS.c.incident_group if group_column else text("NULL")
    rows = conn.execute(select(EVENTS.c.id, EVENTS.c.channel_id, EVENTS.c.ts, EVENTS.c.alarm,
                               EVENTS.c.val_raw, EVENTS.c.val_num, EVENTS.c.event_class,
                               EVENTS.c.hint, group_col)
                        .where(EVENTS.c.ts >= to_db(start - MARGIN),
                               EVENTS.c.ts < to_db(end + MARGIN))).all()
    verdicts: dict[int, list] = {}
    stored: dict[int, tuple] = {}
    series: list[semantics.SeriesEvent] = []
    for row_id, channel_id, ts, alarm, val_raw, val_num, cls, hint, group in rows:
        sensor_type, obj_id, complex_id = channels.get(channel_id, (None, None, None))
        local = from_db(ts)
        verdict = semantics.classify(sensor_type, val_raw, val_num, alarm, ts=local)
        key = semantics.series_key(verdict.incident_group, obj_id, complex_id)
        if key is not None:
            series.append(semantics.SeriesEvent(ref=row_id, group=verdict.incident_group,
                                                key=key, channel_id=channel_id, ts=local))
        if start <= local < end:
            verdicts[row_id] = list(verdict)
            stored[row_id] = (cls, hint, group)
    for row_id, hint in semantics.series_hints(series).items():
        if row_id in verdicts:
            verdicts[row_id][1] = hint
    totals = Totals(rows=len(verdicts))
    changes = []
    for row_id, (cls, hint, group) in verdicts.items():
        totals.groups[group] += 1
        totals.hints[hint_kind(hint)] += 1
        old_cls, old_hint, old_group = stored[row_id]
        if (cls, hint, group) != (old_cls, old_hint, old_group):
            if cls != old_cls:
                totals.classes[(old_cls, cls)] += 1
            totals.hints_changed += hint != old_hint
            totals.groups_changed += group != old_group
            changes.append({"row_id": row_id, "cls": cls, "hint": hint, "grp": group})
    totals.changed = len(changes)
    if write and changes:
        conn.execute(EVENTS.update().where(EVENTS.c.id == bindparam("row_id"))
                     .values(event_class=bindparam("cls"), hint=bindparam("hint"),
                             incident_group=bindparam("grp")), changes)
    return totals


def days_of(conn) -> tuple[date, date] | None:
    first, last = conn.execute(select(func.min(EVENTS.c.ts), func.max(EVENTS.c.ts))).one()
    if first is None:
        return None
    return from_db(first).date(), from_db(last).date()


def reclassify(engine: Engine, date_from: date | None = None, date_to: date | None = None,
               *, dry_run: bool = False, out=print) -> Totals:
    if not dry_run and ensure_column(engine):
        out("добавлены events.incident_group и индекс ix_events_incident_group")
    group_column = has_column(engine)
    with engine.connect() as conn:
        span = days_of(conn)
        channels = channel_map(conn)
    totals = Totals()
    if span is None:
        out("событий нет")
        return totals
    day, last = date_from or span[0], date_to or span[1]
    started = time.monotonic()
    while day <= last:
        tick = time.monotonic()
        with engine.begin() as conn:
            result = reclassify_day(conn, day, channels, group_column=group_column,
                                    write=not dry_run)
        totals.add(result)
        out(f"{day}: строк {result.rows}, изменено {result.changed}, "
            f"{time.monotonic() - tick:.1f} с")
        day += timedelta(days=1)
    elapsed = time.monotonic() - started
    out(f"итого: строк {totals.rows}, изменено {totals.changed}, {elapsed:.1f} с, "
        f"{totals.rows / elapsed if elapsed else 0:.0f} строк/с"
        + (" (dry-run, без записи)" if dry_run else ""))
    out(f"сменился класс {sum(totals.classes.values())}: " + ", ".join(
        f"{old}→{new} {n}" for (old, new), n in totals.classes.most_common()))
    out(f"сменилась подсказка {totals.hints_changed}, группа {totals.groups_changed}")
    out("группы: " + ", ".join(f"{g} {n}" for g, n in totals.groups.most_common() if g))
    out("подсказки: " + ", ".join(f"{k} {n}" for k, n in totals.hints.most_common()
                                  if k != "нет"))
    return totals


def main() -> None:
    safe_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat,
                        help="первые сутки МСК; по умолчанию — первые сутки журнала")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat,
                        help="последние сутки МСК включительно; по умолчанию — последние")
    parser.add_argument("--dry-run", action="store_true",
                        help="посчитать изменения, ничего не записывая")
    args = parser.parse_args()
    url = env_value("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL не задан ни в окружении, ни в .env")
    engine = create_engine(url)
    try:
        reclassify(engine, args.date_from, args.date_to, dry_run=args.dry_run)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
