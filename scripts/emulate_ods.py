"""Эмулятор журнала ОДС — владелец ML2-11 (C2 POST /ingest/ods-journal; QA, тема
«Журнал ОДС»).

Журнал ОДС заказчик не выдал, поэтому скрипт пишет записи за демо-день по тому, что уже
лежит в сервисе:

- прогноз, действующий в этот день, без заявки или с черновиком заявки, — решение
  дежурного ОДС: «выезд», «дистанционная проверка», «отложено» или «отказ»; после
  выезда — «осмотр» с итогом и «закрытие», после дистанционной проверки — «закрытие»
  с итогом. Решение, уже принятое в сервисе, переносится как есть, иначе выбирается
  по весам ACTION_WEIGHTS, причина — из справочника C3, подходящая к действию. Итог
  следует факту по данным СМВУ, если он известен. Прогнозы идут по рангу, пока
  записей меньше --count;
- заявка в работе — «выезд», выполненная или отменённая — «закрытие». Время — из истории
  заявки, перенесённое на демо-день; эти записи идут сверх --count и только за день,
  равный demo_today стенда (GET /system/status): иначе прогон за архивный день задвоил
  бы те же переходы.

Каждая запись помечена: reason начинается с «эмуляция ОДС:». Время и выбор задаёт --seed;
приём отбрасывает точные повторы, поэтому повторный запуск с тем же --seed не добавляет
строк. В --loop скрипт раз в --interval секунд перечитывает прогнозы и заявки и шлёт
записи, чьё время суток уже наступило по часам МСК.

Пишет ключ integration (X-API-Key), читает демо-пользователь --reader (по умолчанию
dispatcher, пароль DEMO_PASSWORD): права view у integration нет.

    python scripts/emulate_ods.py --day 2026-06-30 --count 20
    python scripts/emulate_ods.py --day 2026-06-30 --dry-run
    python scripts/emulate_ods.py --loop --interval 60
"""
import argparse
import random
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as day_time
from urllib.parse import quote

from _api import DEFAULT_BASE_URL, ApiError, safe_console
from _emulation import MSK, SetupError, connect, load_vocab, read_all

MARK = "эмуляция ОДС"
DEFAULT_DAY = date(2026, 6, 30)  # «сегодня» демо-стенда (решение D6)
BATCH_ROWS = 500                 # строк в POST; C5 допускает до 5 000
LOOKBACK_DAYS = 10               # недельный прогноз действует с asof+2 по asof+9
RECORD_BY_ACTION = {"dispatch_crew": "выезд", "remote_check": "дистанционная проверка",
                    "defer": "отложено", "reject": "отказ"}
# Веса, когда решения по прогнозу в сервисе нет. Оценка команды, не статистика ОДС.
ACTION_WEIGHTS = {"dispatch_crew": 3, "remote_check": 4, "defer": 1, "reject": 2}
OUTCOME_WEIGHTS = {"confirmed_event": 3, "no_event": 4, "normal_activation": 2,
                   "sensor_fault": 1}
# Статус заявки из истории → запись ОДС: (тип записи, решение, текст).
ORDER_RECORDS = {
    "in_progress": ("выезд", "dispatch_crew", "бригада выехала по заявке {id}"),
    "completed": ("закрытие", None, "работы по заявке {id} выполнены"),
    "cancelled": ("закрытие", None, "заявка {id} отменена"),
}


def parse_ts(value: str) -> datetime:
    """Время из C2 — в +03:00; без таймзоны — это Москва (правило C1)."""
    ts = datetime.fromisoformat(value)
    return (ts if ts.tzinfo else ts.replace(tzinfo=MSK)).astimezone(MSK)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, day_time(0), MSK)
    return start, start + timedelta(days=1)


def active_on(forecast: dict, day: date) -> bool:
    start, end = day_bounds(day)
    return parse_ts(forecast["valid_from"]) < end and parse_ts(forecast["valid_to"]) > start


def record(ts: datetime, obj_id: str | None, record_type: str, decision: str | None,
           text: str) -> dict:
    """Строка OdsRowIn."""
    return {"ts": ts.isoformat(timespec="seconds"), "obj_id": obj_id,
            "record_type": record_type, "decision": decision, "reason": f"{MARK}: {text}"}


def _weighted(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _decision(forecast: dict, rng: random.Random, vocab: dict) -> tuple[str, str]:
    decided = forecast.get("decision")
    if decided:
        return decided["action"], decided["reason_code"]
    action = _weighted(rng, ACTION_WEIGHTS)
    reasons = [item["code"] for item in vocab["reason_code"] if action in item["actions"]]
    return action, rng.choice(reasons)


def _outcome(forecast: dict, rng: random.Random) -> str:
    if forecast.get("outcome_manual"):
        return forecast["outcome_manual"]
    if forecast.get("outcome_auto") == "hit":
        return "confirmed_event"
    if forecast.get("outcome_auto") == "miss":
        return rng.choice(["no_event", "normal_activation"])
    return _weighted(rng, OUTCOME_WEIGHTS)


def _title(vocab: dict, name: str, code: str) -> str:
    return next((item["title"] for item in vocab[name] if item["code"] == code), code)


def forecast_records(forecast: dict, day: date, *, seed: int, vocab: dict) -> list[dict]:
    """Цепочка записей ОДС по одному прогнозу; всё, что вышло за полночь, отбрасывается."""
    rng = random.Random(f"{seed}|{day.isoformat()}|{forecast['id']}")
    start, end = day_bounds(day)
    first = max(start, parse_ts(forecast["valid_from"]))
    window = int((end - first).total_seconds() // 60)
    if window <= 0:
        return []
    t0 = first + timedelta(minutes=rng.randrange(window))
    action, reason_code = _decision(forecast, rng, vocab)
    outcome = _title(vocab, "outcome_manual", _outcome(forecast, rng))
    about = f"прогноз {forecast['id']}, {forecast.get('scenario_title') or forecast['scenario']}"
    rows = [(t0, RECORD_BY_ACTION[action], action,
             f"{_title(vocab, 'reason_code', reason_code)}; {about}")]
    if action == "dispatch_crew":
        t1 = t0 + timedelta(minutes=rng.randint(40, 150))
        rows.append((t1, "осмотр", None, f"{outcome}; {about}"))
        rows.append((t1 + timedelta(minutes=rng.randint(5, 30)), "закрытие", None,
                     f"выезд завершён; {about}"))
    elif action == "remote_check":
        rows.append((t0 + timedelta(minutes=rng.randint(10, 40)), "закрытие", None,
                     f"{outcome}; {about}"))
    obj_id = (forecast.get("object") or {}).get("id")
    return [record(ts, obj_id, kind, decision, text)
            for ts, kind, decision, text in rows if ts < end]


def order_records(card: dict, day: date) -> list[dict]:
    """«Выезд» и «закрытие» по истории заявки: время суток перехода — на демо-день."""
    _, end = day_bounds(day)
    obj_id = (card.get("object") or {}).get("id")
    rows = []
    last: datetime | None = None
    for item in card.get("history") or []:
        kind = ORDER_RECORDS.get(item["to_status"])
        if kind is None:
            continue
        at = parse_ts(item["at"]).time().replace(microsecond=0)
        ts = datetime.combine(day, at, MSK)
        if last is not None and ts <= last:  # переход через полночь: порядок важнее часов
            ts = min(last + timedelta(minutes=1), end - timedelta(minutes=1))
        last = ts
        record_type, decision, text = kind
        rows.append(record(ts, obj_id, record_type, decision, text.format(id=card["id"])))
    return rows


def plan_day(day: date, forecasts: list[dict], orders: list[dict], *, seed: int, count: int,
             vocab: dict, with_orders: bool = True) -> list[dict]:
    """Записи ОДС за день: по заявкам — все, по прогнозам — не больше count.

    Прогноз, чья заявка ушла дальше черновика, описывает история заявки. Черновик
    заявки создаёт дневной расчёт, а не диспетчер, поэтому такой прогноз получает
    цепочку решения, как прогноз без заявки. Записи по заявкам вызывающий включает
    только за «сегодня» демо-стенда: help desk двигает заявки сейчас, и за архивный
    день те же переходы задвоились бы."""
    rows: list[dict] = []
    covered: set[str] = set()
    for order in sorted(orders, key=lambda item: item["id"]):
        if order["status"] != "draft":
            covered.update(order.get("forecast_ids") or [])
        if with_orders:
            rows.extend(order_records(order, day))
    candidates = sorted((f for f in forecasts if active_on(f, day) and f["id"] not in covered),
                        key=lambda f: (f["rank"], f["id"]))
    budget = count
    for forecast in candidates:
        if budget <= 0:
            break
        chain = forecast_records(forecast, day, seed=seed, vocab=vocab)[:budget]
        rows.extend(chain)
        budget -= len(chain)
    return sorted(rows, key=lambda row: (row["ts"], row["obj_id"] or "", row["record_type"]))


def due(rows: list[dict], day: date, now: datetime) -> list[dict]:
    """Записи, чьё время суток на демо-дне уже наступило по часам МСК."""
    cutoff = datetime.combine(day, now.astimezone(MSK).time(), MSK)
    return [row for row in rows if parse_ts(row["ts"]) <= cutoff]


def collect(reader, day: date) -> tuple[list[dict], list[dict], bool]:
    """Прогнозы, которые могут действовать в день, заявки и признак «день — сегодня
    демо-стенда». За сегодня у заявок после подтверждения берутся карточки с историей."""
    _, status = reader.call("GET", "/system/status")
    is_today = status.get("demo_today") == day.isoformat()
    forecasts = read_all(reader, f"/forecasts?from={day - timedelta(days=LOOKBACK_DAYS)}"
                                 f"&to={day}")
    orders = []
    for order in read_all(reader, "/work-orders"):
        if is_today and order["status"] in ORDER_RECORDS:
            _, order = reader.call("GET", f"/work-orders/{quote(order['id'], safe='')}")
        orders.append(order)
    return forecasts, orders, is_today


def post(writer, rows: list[dict]) -> dict[str, int]:
    totals = {"accepted": 0, "duplicates": 0, "rejected": 0}
    for i in range(0, len(rows), BATCH_ROWS):
        _, batch = writer.call("POST", "/ingest/ods-journal", rows[i:i + BATCH_ROWS])
        for key in totals:
            totals[key] += batch.get(key, 0)
    return totals


def row_key(row: dict) -> tuple:
    return (row["ts"], row["obj_id"], row["record_type"], row["decision"], row["reason"])


def print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(f"{row['ts']}  {row['obj_id'] or '—'}  {row['record_type']}  "
              f"{row['decision'] or '—'}  {row['reason']}")


def print_totals(totals: dict[str, int]) -> None:
    print(f"принято {totals['accepted']}, дублей {totals['duplicates']}, "
          f"отклонено {totals['rejected']}")


def run_once(reader, writer, args, vocab: dict) -> int:
    forecasts, orders, is_today = collect(reader, args.day)
    rows = plan_day(args.day, forecasts, orders, seed=args.seed, count=args.count, vocab=vocab,
                    with_orders=is_today)
    print(f"{args.day}: прогнозов {len(forecasts)}, заявок {len(orders)}, записей ОДС {len(rows)}"
          + ("" if is_today else "; день не «сегодня» стенда — записей по заявкам нет"))
    print_rows(rows)
    if rows and not args.dry_run:
        print_totals(post(writer, rows))
    return 0


def run_loop(reader, writer, args, vocab: dict, *, now=lambda: datetime.now(MSK),
             sleep=time.sleep) -> int:
    print(f"{args.day}: записи по часам МСК, опрос каждые {args.interval:g} с, выход — Ctrl+C")
    sent: set[tuple] = set()
    try:
        while True:
            try:
                forecasts, orders, is_today = collect(reader, args.day)
                rows = plan_day(args.day, forecasts, orders, seed=args.seed,
                                count=args.count, vocab=vocab, with_orders=is_today)
                fresh = [row for row in due(rows, args.day, now()) if row_key(row) not in sent]
                print_rows(fresh)
                if fresh and not args.dry_run:
                    print_totals(post(writer, fresh))
                sent.update(row_key(row) for row in fresh)
            except (ApiError, OSError) as exc:
                print(f"проход не удался: {exc}", file=sys.stderr)
            sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    safe_console()
    sys.stdout.reconfigure(line_buffering=True)  # --loop пишет в лог построчно
    parser = argparse.ArgumentParser(description="Эмулятор журнала ОДС (ML2-11).")
    parser.add_argument("--api", "--base-url", dest="api", default=DEFAULT_BASE_URL,
                        help="адрес api")
    parser.add_argument("--api-key", default=None,
                        help="X-API-Key роли integration (по умолчанию INTEGRATION_API_KEY "
                             "из окружения или .env)")
    parser.add_argument("--reader", default="dispatcher",
                        help="демо-пользователь для чтения прогнозов и заявок "
                             "(пароль — DEMO_PASSWORD)")
    parser.add_argument("--day", type=date.fromisoformat, default=DEFAULT_DAY,
                        help=f"демо-день журнала (по умолчанию {DEFAULT_DAY})")
    parser.add_argument("--count", type=int, default=20,
                        help="сколько записей по прогнозам; записи по заявкам — сверх")
    parser.add_argument("--seed", type=int, default=42, help="зерно генератора")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="записи за весь день одним проходом (так и по умолчанию)")
    mode.add_argument("--loop", action="store_true",
                      help="слать записи по мере наступления их времени суток, до Ctrl+C")
    parser.add_argument("--interval", type=float, default=60, help="период опроса в --loop, с")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать записи")
    args = parser.parse_args()
    if args.count < 0 or args.interval <= 0:
        parser.error("--count не меньше нуля, --interval больше нуля")

    try:
        reader, writer, _ = connect(args.api, args.api_key, args.reader, "ingest")
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 1
    vocab = load_vocab()
    if args.loop:
        return run_loop(reader, writer, args, vocab)
    try:
        return run_once(reader, writer, args, vocab)
    except (ApiError, OSError) as exc:
        print(f"проход не удался: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
