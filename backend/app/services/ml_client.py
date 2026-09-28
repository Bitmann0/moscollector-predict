"""HTTP-клиент ML-сервиса (контракт C1). Живое.

Фронт никогда не ходит в ML напрямую (решение D3): расчёт дня с факторами занимает
около 8 с, до 11 с (docs/submission/08-performance.md), поэтому ML вызывает только
дневной цикл, а интерфейс читает PostgreSQL.
Вызовы последовательные; замок против параллельных расчётов живёт внутри ML.
"""
from datetime import date
from functools import lru_cache

import httpx

from ..config import get_settings
from ..schemas.ml import (
    OutcomeQuery,
    OutcomeResult,
    ReadyResponse,
    ScoreRequest,
    ScoreResponse,
    WeeklyResponse,
)


class MlUnavailable(RuntimeError):
    """ML не ответил или ответил ошибкой. detail идёт в статус прогона."""


class MlClient:
    def __init__(self, base_url: str, timeout: float,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    @classmethod
    def from_http(cls, http: httpx.Client) -> "MlClient":
        """Поверх готового клиента — например starlette TestClient(ml_app) в тестах и
        в scripts/export_contracts.py, чтобы гонять ML-заглушку в одном процессе."""
        client = cls.__new__(cls)
        client._http = http
        return client

    def _get(self, path: str, **params) -> dict:
        return self._call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def _call(self, method: str, path: str, **kw) -> dict | list:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise MlUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise MlUnavailable(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:  # например, ML_URL указывает не на ML и отдаёт HTML
            raise MlUnavailable(f"ответ не JSON: {resp.text[:200]}") from exc

    def health(self) -> dict:
        return self._get("/health")

    def ready(self, asof: date | None = None, timeout: float = 5.0) -> ReadyResponse:
        """Короткий таймаут: /system/status не должен ждать зависший ML две минуты."""
        params = {"asof": asof.isoformat()} if asof else {}
        return ReadyResponse.model_validate(
            self._call("GET", "/ready", params=params, timeout=timeout))

    def score(self, request: ScoreRequest) -> ScoreResponse:
        body = request.model_dump(mode="json")
        return ScoreResponse.model_validate(self._call("POST", "/api/v1/score", json=body))

    def weekly(self, asof: date) -> WeeklyResponse:
        return WeeklyResponse.model_validate(
            self._get("/api/v1/guard-weekly-inspections", asof=asof.isoformat()))

    def outcomes(self, items: list[OutcomeQuery]) -> list[OutcomeResult]:
        body = [item.model_dump(mode="json") for item in items]
        data = self._call("POST", "/api/v1/outcomes", json=body)
        return [OutcomeResult.model_validate(x) for x in data]


@lru_cache
def get_ml_client() -> MlClient:
    """Зависимость FastAPI; тесты подменяют её через app.dependency_overrides."""
    settings = get_settings()
    return MlClient(settings.ml_url, settings.ml_timeout_s)
