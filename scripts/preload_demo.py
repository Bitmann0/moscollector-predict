"""ЗАГЛУШКА — владелец PM-09 (C1 → C2; раздел 3 плана команды, «Демо-режимы стенда»).
Заменить: очистку issued_log окна перед прогоном; history_complete_from=2026-05-25;
недельную очередь за все понедельники 2026-01-05…06-29; запрос факта (ML /outcomes)
для прогнозов с valid_to ≤ demo_now; засев эмулированных решений и итогов проверки
с source=emulated.
Контракт: CLI (--base-url, --from, --to) и порядок вызовов — POST /api/v1/admin/run-daily
по дням строго по возрастанию — не меняются; тест scripts/smoke_compose.py должен
остаться зелёным.

Уже живое: вход admin, прогон run-daily по каждому дню окна, печать статусов голов.
Правила журнала выданного применяет сам run_daily (services/daily_run.py).

    python scripts/preload_demo.py                       # 2026-06-01…2026-06-29
    python scripts/preload_demo.py --from 2026-06-22 --dry-run
"""
import argparse
import sys
import time
from datetime import date, timedelta

from _api import DEFAULT_BASE_URL, Api, ApiError, env_value, safe_console

WINDOW_FROM = date(2026, 6, 1)
WINDOW_TO = date(2026, 6, 29)  # 06-30 не считается: «сегодня» — это расчёт за 06-29 (D6)


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


def main() -> int:
    safe_console()
    parser = argparse.ArgumentParser(
        description="Предзаполнение журнала прогнозов демо-окна (PM-09).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat,
                        default=WINDOW_FROM, help=f"первый день (по умолчанию {WINDOW_FROM})")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, default=WINDOW_TO,
                        help=f"последний день (по умолчанию {WINDOW_TO})")
    parser.add_argument("--timeout", type=float, default=300,
                        help="таймаут одного run-daily, с (холодный расчёт ML — до 30 с)")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать план")
    args = parser.parse_args()

    window = days(args.date_from, args.date_to)
    if not window:
        print("--from позже --to", file=sys.stderr)
        return 2
    print(f"окно {args.date_from}…{args.date_to}, дней: {len(window)}; "
          f"POST /api/v1/admin/run-daily на {args.base_url}")
    print("не сделано (PM-09): очистка issued_log, недельная очередь за полугодие, факт, "
          "эмулированные решения")
    if args.dry_run:
        return 0

    password = env_value("DEMO_PASSWORD")
    if not password:
        print("DEMO_PASSWORD не задан ни в окружении, ни в .env", file=sys.stderr)
        return 1
    api = Api(args.base_url, timeout=args.timeout)
    try:
        api.login("admin", password)
    except (ApiError, OSError) as exc:
        print(f"вход admin не удался: {exc}", file=sys.stderr)
        return 1

    failed = 0
    started = time.monotonic()
    for day in window:
        t0 = time.monotonic()
        try:
            _, out = api.call("POST", "/admin/run-daily", {"asof": day.isoformat()})
        except (ApiError, OSError) as exc:
            failed += 1
            print(f"{day}  FAIL {exc}")
            continue
        heads = ", ".join(f"{name}={head.get('result_status')}"
                          for name, head in sorted((out.get("heads") or {}).items()))
        print(f"{day}  run_id={out.get('run_id')}  {heads}  "
              f"прогнозов={out.get('forecasts_upserted')}  {time.monotonic() - t0:.1f} с")
    print(f"готово за {time.monotonic() - started:.0f} с, ошибок: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
