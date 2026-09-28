"""Предзаполнение демо-стенда (PM-09, раздел 3 плана команды «Демо-режимы стенда»). Живое.

Вызовы API под admin, по порядку:
1. DELETE /admin/issued-log — журнал выданного за окно. Если окно начинается не позже
   06-01, стирается и неделя до него: до 06-01 сервис ничего не выдавал.
2. POST /admin/run-daily {"weekly_only": true} — недельная очередь guard_weekly за
   понедельники с --weekly-from (2026-01-05) до начала окна; дневные головы (A_link, D,
   B, E) на эти даты не считаются.
3. POST /admin/run-daily по каждому дню окна строго по возрастанию. run_daily ставит
   history_complete_from = asof − 7: на 06-01 это 2026-05-25, а дальше журнал полон по
   построению — окно очищено и проходится по порядку. Понедельники окна дают недельную
   очередь тем же вызовом, факт по созревшим прогнозам (ML /outcomes) run_daily
   запрашивает сам.
4. POST /admin/emulate-decisions за --weekly-from…--to — решения и итоги проверки по
   факту с source=emulated, заявки идут за решениями (подтверждена → в работе →
   выполнена или отменена); решения, итоги и заявки людей не трогаются.

Повторный запуск приводит стенд к тому же состоянию: журнал выданного переписывается,
прогнозы и черновики обновляются по id, эмуляция сводится к тому же набору. Код выхода 1 —
упал вызов или голова вернула result_status=error.

    python scripts/preload_demo.py                       # 2026-06-01…2026-06-29
    python scripts/preload_demo.py --from 2026-06-22 --dry-run
"""
import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta

from _api import DEFAULT_BASE_URL, Api, ApiError, env_value, safe_console

WINDOW_FROM = date(2026, 6, 1)
WINDOW_TO = date(2026, 6, 29)  # 06-30 не считается: «сегодня» — это расчёт за 06-29 (D6)
WEEKLY_FROM = date(2026, 1, 5)  # первый понедельник полугодия (D6)
HISTORY_DAYS = 7                # как daily_run.HISTORY_DAYS


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


@dataclass(frozen=True)
class Plan:
    clear_from: date
    clear_to: date
    weekly: list[date]   # понедельники до окна: только недельная очередь
    daily: list[date]    # дни окна: полный run-daily
    emulate_from: date
    emulate_to: date

    @classmethod
    def build(cls, date_from: date, date_to: date, weekly_from: date = WEEKLY_FROM) -> "Plan":
        clear_from = date_from - timedelta(HISTORY_DAYS) if date_from <= WINDOW_FROM \
            else date_from
        weekly = [d for d in days(weekly_from, date_from - timedelta(1)) if d.weekday() == 0]
        return cls(clear_from=clear_from, clear_to=date_to, weekly=weekly,
                   daily=days(date_from, date_to), emulate_from=min(weekly_from, date_from),
                   emulate_to=date_to)


def _heads(out: dict) -> tuple[str, list[str]]:
    heads = sorted((out.get("heads") or {}).items())
    errors = [name for name, head in heads if head.get("result_status") == "error"]
    return ", ".join(f"{name}={head.get('result_status')}" for name, head in heads), errors


def _run(api: Api, day: date, weekly_only: bool, timeout: float | None) -> bool:
    body = {"asof": day.isoformat(), **({"weekly_only": True} if weekly_only else {})}
    t0 = time.monotonic()
    try:
        _, out = api.call("POST", "/admin/run-daily", body, timeout=timeout)
    except (ApiError, OSError) as exc:
        print(f"{day}  FAIL {exc}")
        return False
    heads, errors = _heads(out)
    mark = "  ОШИБКА " + ", ".join(errors) if errors else ""
    print(f"{day}  {'неделя ' if weekly_only else ''}run_id={out.get('run_id')}  {heads}  "
          f"прогнозов={out.get('forecasts_upserted')}  "
          f"заявок={out.get('work_orders_upserted')}  {time.monotonic() - t0:.1f} с{mark}")
    for name in errors:
        print(f"    {name}: {(out['heads'][name].get('detail') or '')[:300]}")
    return not errors


def _total(api: Api, path: str) -> int | str:
    try:
        _, page = api.call("GET", path)
    except (ApiError, OSError):
        return "?"
    return page.get("total", "?")


def preload(api: Api, plan: Plan, timeout: float | None = None) -> int:
    """Прогон плана через API. Возвращает число неудачных шагов."""
    started = time.monotonic()
    try:
        _, cleared = api.call("DELETE", f"/admin/issued-log?from={plan.clear_from}"
                                        f"&to={plan.clear_to}")
    except (ApiError, OSError) as exc:
        print(f"очистка issued_log не удалась: {exc}", file=sys.stderr)
        return 1
    print(f"issued_log {plan.clear_from}…{plan.clear_to}: удалено {cleared.get('deleted')}")

    failed = sum(not _run(api, day, True, timeout) for day in plan.weekly)
    failed += sum(not _run(api, day, False, timeout) for day in plan.daily)

    try:
        _, emu = api.call("POST", "/admin/emulate-decisions",
                          {"date_from": plan.emulate_from.isoformat(),
                           "date_to": plan.emulate_to.isoformat()})
        print(f"эмуляция {plan.emulate_from}…{plan.emulate_to}: с фактом {emu['with_fact']}, "
              f"решений {emu['decisions']} (добавлено {emu['created']}, удалено {emu['removed']}), "
              f"итогов проверки {emu['outcomes']}, заявок сдвинуто из черновика "
              f"{emu.get('work_orders', 0)}, решения людей не тронуты: {emu['skipped_live']}")
    except (ApiError, OSError) as exc:
        failed += 1
        print(f"эмуляция решений не удалась: {exc}")

    window = f"from={plan.emulate_from}&to={plan.emulate_to}&page_size=1"
    print(f"журнал {plan.emulate_from}…{plan.emulate_to}: "
          f"прогнозов {_total(api, f'/forecasts?{window}')}, "
          f"с решением {_total(api, f'/forecasts?{window}&decision=any')}, "
          f"без решения {_total(api, f'/forecasts?{window}&decision=none')}, "
          f"заявок всего {_total(api, '/work-orders?page_size=1')}")
    print(f"готово за {time.monotonic() - started:.0f} с, ошибок: {failed}")
    return failed


def main() -> int:
    safe_console()
    parser = argparse.ArgumentParser(
        description="Предзаполнение демо-стенда: журнал прогнозов, факт, эмулированные "
                    "решения (PM-09).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat,
                        default=WINDOW_FROM, help=f"первый день (по умолчанию {WINDOW_FROM})")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, default=WINDOW_TO,
                        help=f"последний день (по умолчанию {WINDOW_TO})")
    parser.add_argument("--weekly-from", type=date.fromisoformat, default=WEEKLY_FROM,
                        help=f"с какого дня идут понедельники недельной очереди "
                             f"(по умолчанию {WEEKLY_FROM})")
    parser.add_argument("--timeout", type=float, default=300,
                        help="таймаут одного run-daily, с (холодный расчёт ML — до 30 с)")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать план")
    args = parser.parse_args()

    if args.date_from > args.date_to:
        print("--from позже --to", file=sys.stderr)
        return 2
    plan = Plan.build(args.date_from, args.date_to, args.weekly_from)
    print(f"{args.base_url}: очистка issued_log {plan.clear_from}…{plan.clear_to}; "
          f"недельная очередь, понедельников до окна: {len(plan.weekly)}; "
          f"run-daily {args.date_from}…{args.date_to}, дней: {len(plan.daily)}; "
          f"эмуляция решений {plan.emulate_from}…{plan.emulate_to}")
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
    return 1 if preload(api, plan) else 0


if __name__ == "__main__":
    sys.exit(main())
