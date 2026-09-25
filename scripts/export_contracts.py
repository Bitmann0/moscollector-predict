"""Пересобирает содержимое contracts/ из кода. Живое.

    python scripts/export_contracts.py                 # схемы и фикстуры
    python scripts/export_contracts.py --schemas-only  # только схемы
    python scripts/export_contracts.py --strict        # CI: неготовая заглушка — ошибка

CI запускает скрипт и падает, если после него `git diff contracts/` не пуст:
поменял схему — перегенерируй и закоммить, потребитель увидит изменение в PR.

Фикстуры (раздел «Контракты» спецификации каркаса) — в contracts/fixtures/:
- ml_score_2026-06-15.json, ml_guard_weekly_2026-06-15.json — ответы mkl.product_stub;
- ml_outcomes.json — {"request": [...], "response": [...]}: запросы собраны из алертов
  и рекомендаций двух фикстур выше, ответ — product_stub.outcomes;
- api_forecasts.json, api_forecast_card.json, api_dashboard.json, api_events.json —
  ответы backend на временной SQLite после seed и POST /admin/run-daily {asof: 2026-06-15},
  ML — mkl.product_api в том же процессе.

Детерминизм: значения полей с текущим временем (FROZEN_KEYS) заменяются на FROZEN_TS,
ключи словарей сортируются. Если заглушки ML или backend ещё не готовы (ImportError,
NotImplementedError), скрипт предупреждает и оставляет прежние фикстуры; с --strict
завершается с кодом 1.
"""
import argparse
import importlib.util
import json
import os
import runpy
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
FIXTURES = CONTRACTS / "fixtures"
sys.path.insert(0, str(ROOT / "backend"))

FIXTURE_ASOF = date(2026, 6, 15)  # понедельник: есть и дневной расчёт, и недельная очередь
FROZEN_TS = "2026-06-16T00:00:00+03:00"
FROZEN_KEYS = {"created_at", "finished_at", "started_at", "recorded_at", "received_at", "ts",
               "updated_at", "at", "generated_at"}

# Фиктивные значения для backend на временной БД: .env разработчика не должен влиять
# на содержимое фикстур. Это не секреты — БД удаляется сразу после выгрузки.
BACKEND_ENV = {
    "SECRET_KEY": "export-contracts-only",
    "DEMO_PASSWORD": "export-contracts-only",
    "INTEGRATION_API_KEY": "export-contracts-only",
    "DEMO_TODAY": "2026-06-30",
    "DEMO_SETTINGS_LOCKED": "0",
    "SEED_DEMO": "1",
    "COOKIE_SECURE": "0",
    "ML_URL": "http://ml.in-process",
    "CONTRACTS_DIR": str(CONTRACTS),
}


class NotReady(RuntimeError):
    """Заглушка соседней задачи ещё не написана: фикстуры не выгружаются."""


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def freeze(data):
    """Подменяет время выгрузки фиксированной строкой, чтобы git diff был пуст."""
    if isinstance(data, dict):
        return {k: FROZEN_TS if k in FROZEN_KEYS and v is not None else freeze(v)
                for k, v in data.items()}
    if isinstance(data, list):
        return [freeze(item) for item in data]
    return data


def write_fixture(name: str, data) -> None:
    write_json(FIXTURES / name, freeze(data))
    print(f"  fixtures/{name}")


def export_openapi() -> None:
    from app.main import create_app
    write_json(CONTRACTS / "api_v1.openapi.json", create_app().openapi())


def export_ml_schema() -> None:
    spec = importlib.util.spec_from_file_location(
        "ml_product_contract", ROOT / "ml" / "src" / "mkl" / "product_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    models = {name: obj for name, obj in vars(module).items()
              if isinstance(obj, type) and hasattr(obj, "model_json_schema")
              and obj.__module__ == module.__name__}
    from pydantic.json_schema import models_json_schema
    _, schema = models_json_schema([(m, "validation") for m in models.values()],
                                   title="Контракт C1: ML → backend")
    schema["schema_version"] = module.SCHEMA_VERSION
    write_json(CONTRACTS / "ml_v1.schema.json", schema)


def _import_ml(name: str):
    """mkl из рабочей копии в stub-режиме: тяжёлые зависимости ML не нужны."""
    os.environ["ML_MODE"] = "stub"
    ml_src = str(ROOT / "ml" / "src")
    if ml_src not in sys.path:
        sys.path.insert(0, ml_src)
    try:
        return __import__(f"mkl.{name}", fromlist=[name])
    except ImportError as exc:
        raise NotReady(f"mkl.{name}: {exc}") from exc


def export_ml_fixtures() -> None:
    contract = _import_ml("product_contract")
    stub = _import_ml("product_stub")
    try:
        score = stub.score(contract.ScoreRequest(asof=FIXTURE_ASOF))
        weekly = stub.weekly(FIXTURE_ASOF)
        queries = [contract.OutcomeQuery(id=a.alert_id, kind="alert", head=a.head,
                                         channel=a.address.channel, obj=a.address.obj,
                                         asof=a.asof)
                   for a in score.alerts if a.in_budget][:6]
        queries += [contract.OutcomeQuery(id=p.recommendation_id, kind="weekly_recommendation",
                                          head="guard_weekly", obj=p.obj, asof=weekly.asof)
                    for p in weekly.priorities]
        results = stub.outcomes(queries)
    except NotImplementedError as exc:
        raise NotReady(f"mkl.product_stub: {exc}") from exc
    write_fixture(f"ml_score_{FIXTURE_ASOF}.json", score.model_dump(mode="json"))
    write_fixture(f"ml_guard_weekly_{FIXTURE_ASOF}.json", weekly.model_dump(mode="json"))
    write_fixture("ml_outcomes.json", {
        "request": [q.model_dump(mode="json") for q in queries],
        "response": [r.model_dump(mode="json") for r in results],
    })


def _seed() -> None:
    """То же, что `python -m app.seed` в entrypoint.sh контейнера api."""
    argv = sys.argv
    sys.argv = ["app.seed"]
    try:
        runpy.run_module("app.seed", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise RuntimeError(f"app.seed завершился с кодом {exc.code}") from None
    finally:
        sys.argv = argv


def _get(client, path: str, **params) -> dict:
    resp = client.get(path, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def export_backend_fixtures() -> None:
    ml_api = _import_ml("product_api")
    try:
        from app import config, db, models  # noqa: F401 — models регистрирует таблицы
        from app.main import create_app
        from app.services.ml_client import MlClient, get_ml_client
        from fastapi.testclient import TestClient
    except ImportError as exc:
        raise NotReady(f"backend: {exc}") from exc

    tmp = Path(tempfile.mkdtemp(prefix="mk-contracts-"))
    env = {**BACKEND_ENV, "DATABASE_URL": f"sqlite:///{(tmp / 'fixtures.db').as_posix()}"}
    saved_env = {key: os.environ.get(key) for key in env}
    cwd = Path.cwd()
    os.environ.update(env)
    os.chdir(tmp)  # Settings читает .env из текущего каталога: здесь его нет
    config.get_settings.cache_clear()
    db.reset_engine()
    try:
        db.Base.metadata.create_all(db.get_engine())
        _seed()
        app = create_app()
        with TestClient(ml_api.app) as ml_http:
            ml = MlClient.from_http(ml_http)
            app.dependency_overrides[get_ml_client] = lambda: ml
            with TestClient(app) as client:
                resp = client.post("/api/v1/auth/login",
                                   json={"login": "admin", "password": env["DEMO_PASSWORD"]})
                if resp.status_code != 200:
                    raise RuntimeError(f"вход admin: HTTP {resp.status_code}: {resp.text[:300]}")
                resp = client.post("/api/v1/admin/run-daily",
                                   json={"asof": FIXTURE_ASOF.isoformat()})
                if resp.status_code != 200:
                    raise RuntimeError(f"run-daily: HTTP {resp.status_code}: {resp.text[:300]}")
                forecasts = _get(client, "/api/v1/forecasts", page_size=20)
                if not forecasts.get("items"):
                    raise RuntimeError("run-daily на заглушке ML не создал ни одного прогноза")
                card = _get(client, f"/api/v1/forecasts/{forecasts['items'][0]['id']}")
                dashboard = _get(client, "/api/v1/dashboard/summary")
                events = _get(client, "/api/v1/events", page_size=20)
    except NotImplementedError as exc:
        raise NotReady(f"backend: {exc}") from exc
    finally:
        db.reset_engine()
        os.chdir(cwd)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.get_settings.cache_clear()
        shutil.rmtree(tmp, ignore_errors=True)
    write_fixture("api_forecasts.json", forecasts)
    write_fixture("api_forecast_card.json", card)
    write_fixture("api_dashboard.json", dashboard)
    write_fixture("api_events.json", events)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Пересборка contracts/ из кода.")
    parser.add_argument("--schemas-only", action="store_true", help="не выгружать фикстуры")
    parser.add_argument("--strict", action="store_true",
                        help="неготовая заглушка ML или backend — ошибка (так запускает CI)")
    args = parser.parse_args()
    export_openapi()
    export_ml_schema()
    print("схемы: contracts/api_v1.openapi.json, contracts/ml_v1.schema.json")
    if args.schemas_only:
        return 0

    missing = []
    for title, export in [("ML", export_ml_fixtures), ("backend", export_backend_fixtures)]:
        print(f"фикстуры {title}:")
        try:
            export()
        except NotReady as exc:
            missing.append(title)
            print(f"ПРЕДУПРЕЖДЕНИЕ: фикстуры {title} не выгружены, заглушка не готова: {exc}. "
                  "Прежние файлы в contracts/fixtures/ оставлены.", file=sys.stderr)
    if missing and args.strict:
        print(f"--strict: не выгружены фикстуры {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
