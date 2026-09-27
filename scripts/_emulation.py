"""Общее для эмуляторов ML2-11: emulate_ods.py и emulate_helpdesk.py. Владелец ML2-11.

Ключ X-API-Key даёт роль integration, а у неё в contracts/vocabularies.json есть ingest
и work_order_progress, но нет view: GET /forecasts и GET /work-orders отвечают ей 403.
Поэтому у эмулятора два клиента: reader читает под сессией демо-пользователя, writer
пишет с ключом integration. Только stdlib, как и _api.py.
"""
import json
from datetime import timedelta, timezone
from pathlib import Path

from _api import ROOT, Api, ApiError, env_value

MSK = timezone(timedelta(hours=3), "MSK")
VOCAB_PATH = ROOT / "contracts" / "vocabularies.json"
PAGE_SIZE = 500  # предел page_size списков C2


class SetupError(RuntimeError):
    """Эмулятор не может начать работу: нет ключа, пароля или api недоступен."""


def load_vocab(path: Path = VOCAB_PATH) -> dict:
    """Словари C3 — тот же файл, что читает backend."""
    return json.loads(path.read_text(encoding="utf-8"))


def read_all(api, path: str) -> list[dict]:
    """Все страницы списка C2 (Page: items, total); path — от /api/v1, можно с параметрами."""
    sep = "&" if "?" in path else "?"
    items: list[dict] = []
    page = 1
    while True:
        _, body = api.call("GET", f"{path}{sep}page={page}&page_size={PAGE_SIZE}")
        batch = body.get("items") or []
        items.extend(batch)
        if not batch or len(items) >= body.get("total", 0):
            return items
        page += 1


class Reader:
    """Сессия демо-пользователя только для чтения. Сессия живёт SESSION_HOURS (12 ч
    по умолчанию); эмулятор в --loop работает дольше, поэтому после 401 входит заново."""

    def __init__(self, api: Api, login: str, password: str) -> None:
        self.api = api
        self.login = login
        self.password = password

    def call(self, method: str, path: str, body=None, *, timeout: float | None = None):
        try:
            return self.api.call(method, path, body, timeout=timeout)
        except ApiError as exc:
            if exc.status != 401:
                raise
            self.api.login(self.login, self.password)
            return self.api.call(method, path, body, timeout=timeout)


def connect(base_url: str, api_key: str | None, reader_login: str,
            need_perm: str) -> tuple[Reader, Api, frozenset[str]]:
    """Клиенты reader и writer и права ключа по GET /me.

    Права берутся у сервера, а не из словаря: так видно, что ключ принят и что роль
    integration действительно может то, ради чего запущен эмулятор."""
    api_key = api_key or env_value("INTEGRATION_API_KEY")
    if not api_key:
        raise SetupError("ключ integration не задан: --api-key или INTEGRATION_API_KEY "
                         "в окружении или .env")
    writer = Api(base_url, api_key=api_key)
    try:
        _, me = writer.call("GET", "/me")
    except ApiError as exc:
        raise SetupError(f"ключ integration не принят: {exc}") from None
    except OSError as exc:
        raise SetupError(f"api {base_url} недоступен: {exc}") from None
    perms = frozenset(me.get("permissions") or [])
    if need_perm not in perms:
        raise SetupError(f"у роли {me.get('role')} нет права {need_perm}")
    password = env_value("DEMO_PASSWORD")
    if not password:
        raise SetupError(f"DEMO_PASSWORD не задан ни в окружении, ни в .env: под "
                         f"{reader_login} эмулятор читает данные, у integration нет права view")
    reader = Reader(Api(base_url), reader_login, password)
    try:
        reader.api.login(reader_login, password)
    except ApiError as exc:
        raise SetupError(f"вход {reader_login} не удался: {exc}") from None
    return reader, writer, perms
