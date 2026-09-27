"""Эмулятор help desk — владелец ML2-11 (C2 PATCH /work-orders/{id}, C3
work_order_transitions; QA, тема «Интеграция»).

Учётной системы эксплуатации в демо нет, её роль играет этот скрипт: берёт в работу
заявки, которые подтвердил диспетчер, и закрывает их как выполненные.

Шаги строго по графу C3 и правам роли integration: confirmed → in_progress →
completed. Отмену (cancelled) и подтверждение черновика скрипт не делает: оба перехода
требуют work_order_manage, у integration его нет. Права ключа берутся из GET /me.
Заявки читаются под демо-пользователем (--reader, по умолчанию technician, пароль
DEMO_PASSWORD): права view у integration нет.

Каждый PATCH несёт expected_status — статус, который скрипт видел при чтении, и reason
с пометкой «эмуляция help desk» (он попадает в историю заявки). 409 значит, что статус
уже сменил человек: скрипт перечитывает заявку и отсчитывает задержку от нового статуса.
403 и 422 — заявка пропускается, пока её статус не сменится.

Задержка шага зависит от приоритета заявки и повторяется при том же --seed; --speed
делит её (60 — час за минуту). В --once задержки не ждутся: один шаг по каждой заявке.

    python scripts/emulate_helpdesk.py --once
    python scripts/emulate_helpdesk.py --interval 60
    python scripts/emulate_helpdesk.py --loop --speed 60 --interval 5   # для демо
"""
import argparse
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote

from _api import DEFAULT_BASE_URL, ApiError, safe_console
from _emulation import SetupError, connect, load_vocab, read_all

MARK = "эмуляция help desk"
REASONS = {
    "in_progress": f"{MARK}: бригада приняла заявку в работу",
    "completed": f"{MARK}: работы выполнены, заявка закрыта",
}
# Отмена — решение диспетчера или руководителя, а не учётной системы.
NOT_HELPDESK = {"cancelled"}
# Минуты от появления статуса до следующего шага, (от, до) по приоритету заявки.
# Оценка команды: времени реакции бригад в данных заказчика нет.
DELAY_MINUTES = {
    "confirmed": {"urgent": (15, 45), "planned": (60, 240), "watch": (120, 480)},
    "in_progress": {"urgent": (40, 120), "planned": (60, 240), "watch": (60, 240)},
}
DEFAULT_DELAY = (30, 120)


def next_status(status: str, transitions: dict, transition_perm: dict,
                perms: frozenset[str]) -> str | None:
    """Следующий статус, доступный help desk с этими правами, или None."""
    for target in transitions.get(status, []):
        if target in NOT_HELPDESK:
            continue
        need = transition_perm.get(target)
        if need is None or need in perms:
            return target
    return None


def delay_s(seed: int, order_id: str, status: str, priority: str, speed: float) -> float:
    """Задержка шага в секундах: своя у каждой заявки и статуса, та же при том же seed."""
    low, high = DELAY_MINUTES.get(status, {}).get(priority, DEFAULT_DELAY)
    minutes = random.Random(f"{seed}|{order_id}|{status}").uniform(low, high)
    return minutes * 60 / speed


@dataclass
class Seen:
    status: str
    since: float           # время по clock, когда скрипт увидел этот статус
    blocked: bool = False  # 403 или 422: ждать смены статуса


@dataclass(frozen=True)
class Step:
    order_id: str
    from_status: str
    to_status: str
    result: str            # moved, conflict, skipped, gone, planned
    detail: str = ""


class Helpdesk:
    def __init__(self, reader, writer, *, perms, vocab: dict, seed: int = 42,
                 speed: float = 1.0, clock=time.monotonic, dry_run: bool = False) -> None:
        self.reader = reader
        self.writer = writer
        self.perms = frozenset(perms)
        self.transitions = vocab["work_order_transitions"]
        self.transition_perm = vocab["work_order_transition_perm"]
        self.seed = seed
        self.speed = speed
        self.clock = clock
        self.dry_run = dry_run
        self.seen: dict[str, Seen] = {}

    def target(self, status: str) -> str | None:
        return next_status(status, self.transitions, self.transition_perm, self.perms)

    def orders(self) -> list[dict]:
        """Заявки в статусах, из которых у help desk есть шаг."""
        found: list[dict] = []
        for status in self.transitions:
            if self.target(status):
                found.extend(read_all(self.reader, f"/work-orders?status={status}"))
        return sorted(found, key=lambda order: order["id"])

    def step(self, *, wait: bool = True) -> list[Step]:
        """Один проход по заявкам; wait=False — не ждать задержек (--once)."""
        now = self.clock()
        orders = self.orders()
        for order_id in set(self.seen) - {order["id"] for order in orders}:
            del self.seen[order_id]
        steps = []
        for order in orders:
            status = order["status"]
            target = self.target(status)
            if target is None:
                continue
            seen = self.seen.get(order["id"])
            if seen is None or seen.status != status:
                seen = self.seen[order["id"]] = Seen(status, now)
            if seen.blocked:
                continue
            if wait and now - seen.since < delay_s(self.seed, order["id"], status,
                                                   order.get("priority", ""), self.speed):
                continue
            steps.append(self._move(order["id"], status, target, now))
        return steps

    def _move(self, order_id: str, status: str, target: str, now: float) -> Step:
        if self.dry_run:
            return Step(order_id, status, target, "planned")
        body = {"expected_status": status, "status": target,
                "reason": REASONS.get(target, MARK)}
        try:
            self.writer.call("PATCH", f"/work-orders/{quote(order_id, safe='')}", body)
        except ApiError as exc:
            if exc.status == 409:
                return self._reread(order_id, status, target, now)
            if exc.status in (403, 422):
                self.seen[order_id].blocked = True
                return Step(order_id, status, target, "skipped", f"HTTP {exc.status}")
            if exc.status == 404:
                self.seen.pop(order_id, None)
                return Step(order_id, status, target, "gone", "HTTP 404")
            raise
        self.seen[order_id] = Seen(target, now)
        return Step(order_id, status, target, "moved")

    def _reread(self, order_id: str, status: str, target: str, now: float) -> Step:
        """409: статус сменил кто-то другой. Свежий статус — из карточки, а не из ответа
        409, и задержка отсчитывается от него: человек только что работал с заявкой."""
        try:
            _, card = self.reader.call("GET", f"/work-orders/{quote(order_id, safe='')}")
        except ApiError as exc:
            if exc.status != 404:
                raise
            self.seen.pop(order_id, None)
            return Step(order_id, status, target, "gone", "HTTP 404")
        self.seen[order_id] = Seen(card["status"], now)
        return Step(order_id, status, target, "conflict", f"статус уже {card['status']}")


RESULT_TITLES = {"moved": "переведена", "conflict": "конфликт", "skipped": "пропущена",
                 "gone": "удалена", "planned": "план"}


def print_steps(steps: list[Step]) -> None:
    for step in steps:
        detail = f" ({step.detail})" if step.detail else ""
        print(f"{step.order_id}  {step.from_status} → {step.to_status}  "
              f"{RESULT_TITLES[step.result]}{detail}")


def loop(desk: Helpdesk, interval: float, sleep=time.sleep) -> int:
    print(f"опрос заявок каждые {interval:g} с, выход — Ctrl+C")
    try:
        while True:
            try:
                print_steps(desk.step())
            except (ApiError, OSError) as exc:
                print(f"проход не удался: {exc}", file=sys.stderr)
            sleep(interval)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    safe_console()
    sys.stdout.reconfigure(line_buffering=True)  # --loop пишет в лог построчно
    parser = argparse.ArgumentParser(description="Эмулятор help desk (ML2-11).")
    parser.add_argument("--api", "--base-url", dest="api", default=DEFAULT_BASE_URL,
                        help="адрес api")
    parser.add_argument("--api-key", default=None,
                        help="X-API-Key роли integration (по умолчанию INTEGRATION_API_KEY "
                             "из окружения или .env)")
    parser.add_argument("--reader", default="technician",
                        help="демо-пользователь для чтения заявок (пароль — DEMO_PASSWORD)")
    parser.add_argument("--interval", type=float, default=60, help="период опроса заявок, с")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="один шаг по каждой заявке без задержек и выход")
    mode.add_argument("--loop", action="store_true",
                      help="опрашивать заявки до Ctrl+C (так и по умолчанию)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="делитель задержек: 1 — реальный темп, 60 — час за минуту")
    parser.add_argument("--seed", type=int, default=42, help="зерно генератора задержек")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать шаги")
    args = parser.parse_args()
    if args.interval <= 0 or args.speed <= 0:
        parser.error("--interval и --speed должны быть больше нуля")

    try:
        reader, writer, perms = connect(args.api, args.api_key, args.reader,
                                        "work_order_progress")
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 1
    desk = Helpdesk(reader, writer, perms=perms, vocab=load_vocab(), seed=args.seed,
                    speed=args.speed, dry_run=args.dry_run)
    if not args.once:
        return loop(desk, args.interval)
    try:
        steps = desk.step(wait=False)
    except (ApiError, OSError) as exc:
        print(f"проход не удался: {exc}", file=sys.stderr)
        return 1
    print_steps(steps)
    totals = Counter(RESULT_TITLES[step.result] for step in steps)
    print("итог: " + (", ".join(f"{name} {n}" for name, n in sorted(totals.items()))
                      or "заявок с доступным шагом нет"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
