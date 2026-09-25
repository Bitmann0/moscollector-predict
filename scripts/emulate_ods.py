"""ЗАГЛУШКА — владелец ML2-11 (C2 /ingest/ods-journal; QA, тема «Журнал ОДС»).
Заменить: генерацию правдоподобных записей журнала ОДС по прогнозам и заявкам дня
(выезд, дистанционная проверка, отказ с причиной) и их отправку в
POST /api/v1/ingest/ods-journal с X-API-Key; детерминизм по --seed.
Контракт: флаги CLI и формат строки OdsRowIn (ts, obj_id, record_type, decision, reason)
не меняются; записи помечаются как эмуляция; тест scripts/smoke_compose.py должен
остаться зелёным.

    python scripts/emulate_ods.py --day 2026-06-30 --count 20
"""
import argparse
import os
import sys
from datetime import date

from _api import safe_console


def main() -> int:
    safe_console()
    parser = argparse.ArgumentParser(description="Эмулятор журнала ОДС (ML2-11).")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="адрес api")
    parser.add_argument("--api-key", default=os.environ.get("INTEGRATION_API_KEY"),
                        help="X-API-Key роли integration (по умолчанию из INTEGRATION_API_KEY)")
    parser.add_argument("--day", type=date.fromisoformat, default=date(2026, 6, 30))
    parser.add_argument("--count", type=int, default=20, help="сколько записей создать")
    parser.add_argument("--seed", type=int, default=42, help="зерно генератора")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать план")
    args = parser.parse_args()

    print("emulate_ods.py — заглушка ML2-11, записи не отправляются. План:")
    print(f"  {args.count} записей ОДС за {args.day}, seed={args.seed}")
    print(f"  POST {args.api}/api/v1/ingest/ods-journal, "
          f"ключ integration: {'задан' if args.api_key else 'НЕ задан'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
