"""Единая точка запуска.

Раньше пайплайн собирался пятью скриптами в правильном порядке, и порядок надо
было знать. Теперь `mkl run` сам решает, что пересобрать: стадия выполняется,
если устарела сама или если пересобрался её вход.

Команды намеренно немногочисленны. Принимающей команде нужно понять сервис за
один присест, а не изучать оркестратор.
"""
import argparse
import datetime as dt
import json
import sys

from . import pipeline


def _status(args) -> int:
    rows = pipeline.status()
    width = max(len(r["stage"]) for r in rows)
    print(f"{'стадия':<{width}}  {'состояние':<14} что делает")
    for r in rows:
        print(f"{r['stage']:<{width}}  {r['state']:<14} {r['title']}")
        if r["note"]:
            print(f"{'':<{width}}  {'':<14} — {r['note']}")
    todo = [s.name for s in pipeline.plan()]
    print("\nк пересборке: " + (", ".join(todo) if todo else "нечего, всё свежее"))
    return 0


def _run(args) -> int:
    todo = pipeline.plan(args.stage, force=args.force)
    if not todo:
        print("всё свежее; для принудительного прогона используйте --force")
        return 0
    print("будут выполнены: " + ", ".join(s.name for s in todo))
    return pipeline.run(todo, dry_run=args.dry_run)


def _alerts(args) -> int:
    from . import service
    got = service.daily_alerts(asof=args.asof, only_in_budget=not args.all,
                               with_factors=not args.no_factors)
    if args.json:
        print(json.dumps([a.to_dict() for a in got], ensure_ascii=False, indent=1))
        return 0
    if not got:
        print("алертов нет")
        return 0
    print(f"{'голова':<9}{'риск':>7}  {'объект':<8}{'канал':>8}  направление")
    for a in got[:args.limit]:
        print(f"{a.head:<9}{a.risk:>7.3f}  {str(a.address.obj or '—'):<8}"
              f"{str(a.address.channel or '—'):>8}  {a.direction_title}")
    if len(got) > args.limit:
        print(f"… ещё {len(got) - args.limit}")
    return 0


def _orders(args) -> int:
    from . import service, workorders
    got = workorders.build(service.daily_alerts(asof=args.asof))
    if args.json:
        print(json.dumps([w.to_dict() for w in got], ensure_ascii=False, indent=1))
        return 0
    print(f"{'номер':<17}{'срочность':<12}{'объект':<8} вид работ")
    for w in got[:args.limit]:
        print(f"{w.order_id:<17}{w.priority:<12}{str(w.obj or '—'):<8} {w.work_type}")
    return 0


def _coverage(args) -> int:
    from . import service
    for c in service.coverage(args.asof):
        print(f"{c.head:<9}{c.entities_scored:>6} из {c.entities_total:<7}"
              f"{c.fraction:>7.2f}  {c.reason or ''}")
    return 0


def _api(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print("веб-часть не установлена: pip install -e .[api]", file=sys.stderr)
        return 1
    uvicorn.run("mkl.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _force_utf8() -> None:
    """Консоль Windows по умолчанию в cp1251 и падает на «×» в названии стадии.

    Падение на выводе — худший вид отказа: работа сделана, а команда завершилась
    ошибкой. Поэтому поток переводится в UTF-8 явно, а не по настроению
    окружения.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    p = argparse.ArgumentParser(
        prog="mkl",
        description="Предиктивная аналитика коллекторов АО «Москоллектор»")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="что устарело и что надо пересобрать")
    s.set_defaults(func=_status)

    s = sub.add_parser("run", help="пересобрать устаревшие стадии")
    s.add_argument("stage", nargs="?", help="до какой стадии включительно")
    s.add_argument("--force", action="store_true", help="пересобрать даже свежее")
    s.add_argument("--dry-run", action="store_true", help="показать команды и выйти")
    s.set_defaults(func=_run)

    s = sub.add_parser("alerts", help="суточная выдача")
    s.add_argument("--asof", type=dt.date.fromisoformat)
    s.add_argument("--all", action="store_true", help="включая то, что вне бюджета")
    s.add_argument("--no-factors", action="store_true", help="без объяснений")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=_alerts)

    s = sub.add_parser("orders", help="заявки на превентивное обслуживание")
    s.add_argument("--asof", type=dt.date.fromisoformat)
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=_orders)

    s = sub.add_parser("coverage", help="по скольким сущностям головы отвечают")
    s.add_argument("--asof", type=dt.date.fromisoformat)
    s.set_defaults(func=_coverage)

    s = sub.add_parser("api", help="поднять REST API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=_api)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
