"""HTTP-клиент скриптов к API C2. Живое.

Только stdlib (urllib, http.cookiejar): smoke_compose.py и preload_demo.py запускаются
без venv — в CI, на стенде, на машине эксперта. Сессия — cookie mk_session после
POST /api/v1/auth/login; машинные клиенты вместо неё шлют X-API-Key.
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 127.0.0.1, а не localhost: urllib пробует ::1 первым, а проброс порта Docker Desktop
# на Windows по ::1 зависал (25.09, Docker Desktop 4.91): /stream не отдавал ни одного
# события, curl к [::1] не получал ответа. Браузер на localhost этим не страдал.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
API = "/api/v1"


def safe_console() -> None:
    """Консоль Windows в cp1251 не печатает часть символов: заменяем их, а не падаем."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def env_value(name: str, env_file: Path = ROOT / ".env") -> str | None:
    """Значение переменной из окружения, иначе из .env в корне репозитория."""
    value = os.environ.get(name)
    if value:
        return value
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip() == name:
            return _dotenv_value(raw) or None
    return None


def _dotenv_value(raw: str) -> str:
    """Как compose читает значение: в кавычках — до закрывающей кавычки, без кавычек —
    до комментария « #». Подстановку $VAR compose делает, мы — нет: поэтому в .env.example
    сказано не использовать $ в паролях."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    comment = raw.find(" #")
    return (raw[:comment] if comment >= 0 else raw).strip()


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class Api:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, api_key: str | None = None,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def open(self, method: str, path: str, body=None, *, accept: str = "application/json",
             timeout: float | None = None):
        """Открывает ответ как есть: для потока (SSE) и HTML. Закрывает вызывающий."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", accept)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            return self._opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise ApiError(method, path, exc.code, text) from None

    def call(self, method: str, path: str, body=None, *,
             timeout: float | None = None) -> tuple[int, object]:
        """JSON-запрос к API: путь от /api/v1. Возвращает (код, тело)."""
        with self.open(method, API + path, body, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)

    def login(self, login: str, password: str) -> dict:
        _, user = self.call("POST", "/auth/login", {"login": login, "password": password})
        return user
