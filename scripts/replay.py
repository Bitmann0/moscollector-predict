"""ЗАГЛУШКА — владелец ML2-03 (C5; C2 /ingest/events, /ingest/day).
Заменить: чтение событий дня из бандла (data/interim/events_year=2026.parquet); подачу
пачками до 5 000 строк в POST /api/v1/ingest/events в темпе --speed; при --align-to-clock —
старт с текущего времени суток МСК; при --loop — в 00:00 DELETE /api/v1/ingest/day/{day}
и новый круг; запись задержки «событие → БД» для ML2-04.
Контракт: флаги CLI и формат пачки (EventRowIn: ид_события, ид_канала_данных, дата, время,
тревожное, значение_датчика) не меняются; роль — integration через X-API-Key;
тест scripts/smoke_compose.py должен остаться зелёным.

    python scripts/replay.py --day 2026-06-30 --loop --align-to-clock
    python scripts/replay.py --speed 600      # сутки за 2,4 мин — только для видео
"""
import argparse
import os
import sys
from datetime import date

from _api import safe_console

DEFAULT_DAY = date(2026, 6, 30)  # «сегодня» демо-стенда (решение D6)
BATCH_LIMIT = 5000               # предел JSON-пачки по C5


def main() -> int:
    safe_console()
    parser = argparse.ArgumentParser(description="Проигрыватель событий дня в API (ML2-03).")
    parser.add_argument("--day", type=date.fromisoformat, default=DEFAULT_DAY,
                        help=f"день журнала (по умолчанию {DEFAULT_DAY})")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="множитель времени: 1 — реальный темп, 600 — сутки за 2,4 мин")
    parser.add_argument("--loop", action="store_true",
                        help="после конца дня удалить его события и начать заново")
    parser.add_argument("--align-to-clock", action="store_true",
                        help="начать с текущего времени суток МСК")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="адрес api")
    parser.add_argument("--api-key", default=os.environ.get("INTEGRATION_API_KEY"),
                        help="X-API-Key роли integration (по умолчанию из INTEGRATION_API_KEY)")
    parser.add_argument("--source", default="bundle/data/interim/events_year=2026.parquet",
                        help="события из бандла C4")
    parser.add_argument("--batch-size", type=int, default=500,
                        help=f"строк в пачке, не больше {BATCH_LIMIT}")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= BATCH_LIMIT:
        parser.error(f"--batch-size от 1 до {BATCH_LIMIT}")

    print("replay.py — заглушка ML2-03, события не отправляются. План:")
    print(f"  источник: {args.source}, день {args.day}")
    print(f"  POST {args.api}/api/v1/ingest/events пачками по {args.batch_size}, "
          f"темп x{args.speed:g}, ключ integration: {'задан' if args.api_key else 'НЕ задан'}")
    if args.align_to_clock:
        print("  старт с текущего времени суток МСК")
    if args.loop:
        print(f"  в 00:00 — DELETE {args.api}/api/v1/ingest/day/{args.day} и новый круг")
    return 0


if __name__ == "__main__":
    sys.exit(main())
