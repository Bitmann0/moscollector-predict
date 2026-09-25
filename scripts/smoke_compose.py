"""Сквозная проверка поднятого compose: шаги 1–8 раздела «Тесты → Смоук» спецификации
каркаса. Живое.

    cp .env.example .env            # вписать DEMO_PASSWORD, SECRET_KEY, INTEGRATION_API_KEY
    docker compose up -d --build --wait
    python scripts/smoke_compose.py

Вход идёт первым шагом: /system/status требует права view. Пароль демо-пользователей
берётся из DEMO_PASSWORD окружения или из .env в корне. Шаги 3 и 6 пишут в БД
(прогон run-daily и решение диспетчера); на стенде их выключает --read-only.
Только stdlib: запуск без venv. Код выхода 1 — упал хотя бы один шаг.
"""
import argparse
import sys
import time
import urllib.error

from _api import DEFAULT_BASE_URL, Api, ApiError, env_value, safe_console

DEMO_ASOF = "2026-06-29"  # последний день предзаполнения (решение D6)


class StepFailed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise StepFailed(message)


def wait_health(api: Api, deadline_s: float) -> str:
    started = time.monotonic()
    last = "нет ответа"
    while time.monotonic() - started < deadline_s:
        try:
            status, _ = api.call("GET", "/health", timeout=3)
            if status == 200:
                return f"health ok за {time.monotonic() - started:.0f} с"
            last = f"HTTP {status}"
        except (ApiError, urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(2)
    raise StepFailed(f"/api/v1/health не ответил за {deadline_s:.0f} с: {last}")


class Smoke:
    def __init__(self, args: argparse.Namespace, password: str) -> None:
        self.args = args
        self.password = password
        self.dispatcher = Api(args.base_url, timeout=args.timeout)
        self.admin = Api(args.base_url, timeout=args.timeout)
        self.forecast_id: str | None = None

    def login_dispatcher(self) -> str:
        user = self.dispatcher.login("dispatcher", self.password)
        check(user.get("role") == "dispatcher", f"роль после входа: {user.get('role')}")
        _, me = self.dispatcher.call("GET", "/me")
        check(me.get("login") == "dispatcher", f"/me вернул {me.get('login')}")
        return f"права: {', '.join(me.get('permissions', []))}"

    def system_status(self) -> str:
        status, body = self.dispatcher.call("GET", "/system/status")
        check(status == 200, f"HTTP {status}")
        ml = body.get("ml") or {}
        return (f"demo_today={body.get('demo_today')}, ML reachable={ml.get('reachable')}, "
                f"mode={ml.get('mode')}, status={ml.get('status')}")

    def run_daily(self) -> str:
        self.admin.login("admin", self.password)
        status, body = self.admin.call("POST", "/admin/run-daily", {"asof": self.args.asof},
                                       timeout=self.args.run_timeout)
        check(status == 200, f"HTTP {status}")
        heads = ", ".join(f"{name}={head.get('result_status')}"
                          for name, head in sorted((body.get("heads") or {}).items()))
        return (f"run_id={body.get('run_id')}, головы: {heads or 'нет'}, "
                f"прогнозов={body.get('forecasts_upserted')}")

    def forecasts(self) -> str:
        _, page = self.dispatcher.call("GET", "/forecasts?page_size=20")
        check(page.get("total", 0) > 0 and page.get("items"),
              "журнал прогнозов пуст: run-daily не создал прогнозов")
        self.forecast_id = page["items"][0]["id"]
        return f"всего {page['total']}, первый {self.forecast_id}"

    def card(self) -> str:
        check(self.forecast_id is not None, "нет id прогноза с шага 4")
        _, card = self.dispatcher.call("GET", f"/forecasts/{self.forecast_id}")
        check(card.get("id") == self.forecast_id, f"карточка вернула id {card.get('id')}")
        return f"{card.get('scenario')}, объект: {(card.get('object') or {}).get('name')}"

    def decision(self) -> str:
        _, reasons = self.dispatcher.call("GET", "/reason-codes")
        reason = next((r for r in reasons if r.get("actions")), None)
        check(reason is not None, "/reason-codes пуст или без действий")
        body = {"action": reason["actions"][0], "reason_code": reason["code"],
                "comment": "smoke_compose.py"}
        status, out = self.dispatcher.call("POST", f"/forecasts/{self.forecast_id}/decisions",
                                           body)
        check(status == 201, f"HTTP {status}")
        check(out.get("action") == body["action"], f"сохранено действие {out.get('action')}")
        return f"решение {out.get('id')}: {body['action']} / {body['reason_code']}"

    def index_html(self) -> str:
        with self.dispatcher.open("GET", "/", accept="text/html") as resp:
            ctype = resp.headers.get("Content-Type", "")
            text = resp.read(4096).decode("utf-8", "replace").lower()
        check(resp.status == 200, f"HTTP {resp.status}")
        check("text/html" in ctype and "<html" in text, f"не index.html: {ctype}")
        return ctype

    def stream(self) -> str:
        started = time.monotonic()
        with self.dispatcher.open("GET", "/api/v1/stream", accept="text/event-stream",
                                  timeout=self.args.timeout) as resp:
            check("text/event-stream" in resp.headers.get("Content-Type", ""),
                  f"Content-Type {resp.headers.get('Content-Type')}")
            while time.monotonic() - started < self.args.timeout:
                raw = resp.readline()
                if not raw:  # конец потока; пустая строка SSE — это b"\n", не b""
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    return f"первое событие «{line.removeprefix('event:').strip()}»"
        raise StepFailed("поток не прислал ни одного события")


def main() -> int:
    safe_console()
    parser = argparse.ArgumentParser(
        description="Сквозная проверка compose по спецификации каркаса (шаги 1–8).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"адрес api (по умолчанию {DEFAULT_BASE_URL})")
    parser.add_argument("--asof", default=DEMO_ASOF, help=f"день run-daily (по умолчанию {DEMO_ASOF})")
    parser.add_argument("--wait", type=float, default=180,
                        help="сколько секунд ждать /api/v1/health (по умолчанию 180)")
    parser.add_argument("--timeout", type=float, default=20, help="таймаут запроса, с")
    parser.add_argument("--run-timeout", type=float, default=300,
                        help="таймаут run-daily, с: холодный расчёт ML в real-режиме долгий")
    parser.add_argument("--read-only", action="store_true",
                        help="не писать в БД: пропустить run-daily (шаг 3) и решение (шаг 6)")
    args = parser.parse_args()

    password = env_value("DEMO_PASSWORD")
    if not password:
        print("DEMO_PASSWORD не задан ни в окружении, ни в .env", file=sys.stderr)
        return 1

    smoke = Smoke(args, password)
    steps = [
        ("вход dispatcher, /me", smoke.login_dispatcher),
        ("GET /system/status = 200", smoke.system_status),
        (f"run-daily под admin на {args.asof}", None if args.read_only else smoke.run_daily),
        ("GET /forecasts не пуст", smoke.forecasts),
        ("карточка прогноза открывается", smoke.card),
        ("решение диспетчера сохраняется", None if args.read_only else smoke.decision),
        ("GET / отдаёт index.html", smoke.index_html),
        ("GET /stream отдаёт первое событие", smoke.stream),
    ]

    try:
        print(f"[0/8] ожидание api {args.base_url}: {wait_health(smoke.dispatcher, args.wait)}")
    except StepFailed as exc:
        print(f"[0/8] FAIL {exc}")
        return 1

    for number, (title, step) in enumerate(steps, start=1):
        if step is None:
            print(f"[{number}/8] SKIP {title} (--read-only)")
            continue
        try:
            detail = step()
        except (StepFailed, ApiError, urllib.error.URLError, OSError, KeyError,
                TypeError) as exc:
            print(f"[{number}/8] FAIL {title}: {type(exc).__name__}: {exc}")
            return 1
        print(f"[{number}/8] ok   {title}: {detail}")
    print("смоук пройден")
    return 0


if __name__ == "__main__":
    sys.exit(main())
