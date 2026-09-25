"""Предел размера тела запроса. Живое.

FastAPI читает тело раньше, чем проверяет вход: без предела анонимный клиент
может загрузить гигабайты во временные файлы и память до ответа 401. Поэтому
предел стоит самым внешним ASGI-слоем: 413 по Content-Length сразу, а для
chunked-передачи — как только счётчик байтов превысит предел. На стенде тот же
предел повторяет Caddy (deploy/Caddyfile: request_body max_size).
"""
import json

UPLOAD_PATH = "/api/v1/ingest/events/upload"
UPLOAD_MAX_BYTES = 200 * 1024 * 1024   # файл журнала СМВУ (routers/ingest.py)
DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # всё остальное: JSON-пачки до 5 000 строк
INGEST_PREFIX = "/api/v1/ingest/"
SESSION_COOKIE = "mk_session"


class BodyTooLarge(Exception):
    pass


def limit_for(path: str) -> int:
    return UPLOAD_MAX_BYTES if path == UPLOAD_PATH else DEFAULT_MAX_BYTES


async def _send_json(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        path = scope["path"]
        limit = limit_for(path)
        headers = {k.lower(): v for k, v in scope["headers"]}
        # Приём журналов — самый тяжёлый путь: без признака входа тело не читаем вовсе.
        if path.startswith(INGEST_PREFIX) and b"x-api-key" not in headers and \
                SESSION_COOKIE.encode() not in headers.get(b"cookie", b""):
            return await _send_json(send, 401, "not_authenticated")
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return await _send_json(send, 413, "request_too_large")

        received = 0
        started = False     # приложение уже начало свой ответ
        rejected = False    # мы ответили 413 сами

        async def limited_receive():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit and not rejected:
                    # Отвечаем 413 сами, а приложению сообщаем об обрыве: FastAPI
                    # превращает исключение в разборе тела в 400, а нам нужен 413.
                    rejected = True
                    if not started:
                        await _send_json(send, 413, "request_too_large")
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started
            if rejected:
                return  # ответ приложения после нашего 413 отбрасывается
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BodyTooLarge:  # на случай, если слой ниже всё же пробросит переполнение
            if not started and not rejected:
                await _send_json(send, 413, "request_too_large")
        except Exception:
            if not rejected:
                raise
