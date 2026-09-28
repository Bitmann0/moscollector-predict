"""Пересчёт класса, подсказки и группы аварии у загруженных событий СМВУ. Живое.

Правила C5 поменялись 28.09 по ответам заказчика (analysis/qa_customer_2026-09-28.md):
группы аварий, «тревожное сообщение», подсказки «вероятно, ППР или ТО». Приём ставит
класс один раз, поэтому события, загруженные раньше, хранят старые класс и подсказку,
а группы у них нет. Скрипт пересчитывает все три поля теми же функциями, что и приём:
classify и series_hints из backend/app/services/semantics.py.

Параметры — текущие из settings (экран «Настройки», ML2-13): пороги метана, окна подсказок
и серий; без сохранённых параметров — проверенные. Код суток общий с эндпоинтом
POST /api/v1/admin/reclassify-events — backend/app/services/reclassify.py.

Скрипт, а не только эндпоинт:
- на стенде работает образ без этой ветки: эндпоинта в нём нет, а контейнеры стенда
  не перезапускаем;
- эндпоинт за вызов пересчитывает не больше 31 суток, чтобы HTTP-запрос не висел
  минуты; вся история стенда — 10 428 318 строк, скрипт прошёл её за 82,1 с
  (docs/submission/08-performance.md).

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
from datetime import date, timedelta
from pathlib import Path

from _api import ROOT, env_value, safe_console
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(ROOT) / "backend"))  # backend/ — не установленный пакет
from app.services import parameters
from app.services.reclassify import Totals, channel_map, days_of, reclassify_day

INDEX = "ix_events_incident_group"


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


def reclassify(engine: Engine, date_from: date | None = None, date_to: date | None = None,
               *, dry_run: bool = False, out=print) -> Totals:
    if not dry_run and ensure_column(engine):
        out("добавлены events.incident_group и индекс ix_events_incident_group")
    group_column = has_column(engine)
    with engine.connect() as conn:
        span = days_of(conn)
        channels = channel_map(conn)
    with Session(engine) as db:
        rules = parameters.load(db).rules
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
                                    write=not dry_run, rules=rules)
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
