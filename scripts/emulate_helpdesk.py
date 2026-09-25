"""ЗАГЛУШКА — владелец ML2-11 (C2 PATCH /work-orders/{id}, C3 work_order_transitions;
QA, тема «Интеграция»).
Заменить: опрос GET /api/v1/work-orders и перевод заявок по графу статусов C3
(confirmed → in_progress → completed, иногда cancelled с причиной) с реалистичными
задержками; обработку 409 при гонке с диспетчером.
Контракт: флаги CLI не меняются; переходы идут через PATCH с expected_status и ключом
роли integration; тест scripts/smoke_compose.py должен остаться зелёным.

    python scripts/emulate_helpdesk.py --interval 60
    python scripts/emulate_helpdesk.py --once
"""
import argparse
import os
import sys

from _api import safe_console


def main() -> int:
    safe_console()
    parser = argparse.ArgumentParser(description="Эмулятор help desk (ML2-11).")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="адрес api")
    parser.add_argument("--api-key", default=os.environ.get("INTEGRATION_API_KEY"),
                        help="X-API-Key роли integration (по умолчанию из INTEGRATION_API_KEY)")
    parser.add_argument("--interval", type=float, default=60, help="период опроса заявок, с")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--seed", type=int, default=42, help="зерно генератора задержек")
    args = parser.parse_args()

    print("emulate_helpdesk.py — заглушка ML2-11, заявки не меняются. План:")
    print(f"  GET {args.api}/api/v1/work-orders "
          f"{'один раз' if args.once else f'каждые {args.interval:g} с'}")
    print("  PATCH /api/v1/work-orders/{id} {expected_status, status, reason}, "
          f"ключ integration: {'задан' if args.api_key else 'НЕ задан'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
