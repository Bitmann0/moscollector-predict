# Каркас продукта — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Цель:** ветка `chore/skeleton`, в которой `docker compose up --build` поднимает весь продукт на заглушках, а каждый участник дописывает свои заглушки, не трогая чужие файлы.

**Архитектура:** монорепо с тремя сервисами.
- `db` — PostgreSQL 16.
- `ml` — FastAPI C1 в `ml/src/mkl/product_api.py`, режимы `ML_MODE=stub|real`.
- `api` — FastAPI C2 в `backend/app` плюс сборка React-фронта в `static/`.

Контракты лежат в `contracts/` и пересобираются скриптом. Внешний интерфейс каждой заглушки окончательный, покрыт тестом формы.

**Стек:**
- Python 3.12, FastAPI, SQLAlchemy 2, Alembic, psycopg 3, pydantic 2, itsdangerous;
- React 18 + Vite + TypeScript, openapi-fetch и openapi-typescript;
- Docker Compose, GitHub Actions.

**Спецификация:** `docs/superpowers/specs/2026-09-25-skeleton-design.md`. Всё, что там сказано, входит в требования каждой задачи.

**Отклонение от формата навыка — осознанное.** Полный код приводится для слоя контрактов (задача 1): от него зависят все. Для заглушек даны точные сигнатуры, ожидаемые ответы и тесты. Код заглушек повторял бы таблицы спецификации.

## Глобальные ограничения

- Рабочая копия — `U:\hackathon-skeleton`, ветка `chore/skeleton`. Коммиты только локальные, пуша нет.
- В `ml/` меняются только новые файлы: `src/mkl/product_contract.py`, `product_stub.py`, `product_api.py`, `tests/test_product_api.py`, `Dockerfile`, `.dockerignore`, `requirements.lock`. Существующие файлы CAML не трогаем.
- Шапка каждой заглушки строится по образцу:

  ```
  ЗАГЛУШКА — владелец <ID> (<контракт>). Заменить: <что>. Контракт: <что не меняется>; тест <файл> должен остаться зелёным.
  ```

- Синтетические сущности называются «Объект-заглушка N» или «Комплекс-заглушка N», в ответах стоит `source="stub"`. Реальных данных заказчика в git нет.
- Пароли в README не пишем, `.env` в git не кладём. Все переменные перечислены в `.env.example` без значений секретов.
- Время: даты без таймзоны в C1 означают Europe/Moscow; бэкенд отдаёт datetime с `+03:00`.
- Сообщения коммитов: префиксы из `CONTRIBUTING.md` и последняя строка `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python-стиль — `ruff`, `line-length 100`.

---

### Задача 1: слой контрактов (выполняет координатор, до остальных)

**Файлы:**
- Create: `contracts/vocabularies.json`, `contracts/README.md`
- Create: `ml/src/mkl/product_contract.py`
- Modify: `pyproject.toml` (зависимости backend)
- Create: `.env.example`, `backend/app/config.py`, `backend/app/db.py`, `backend/app/models.py`, `backend/app/vocab.py`, `backend/app/security.py`
- Create: `backend/app/schemas/*.py`, `backend/app/routers/*.py` (тонкие), `backend/app/services/*.py` — сигнатуры с `raise NotImplementedError`, которые задача 3 заменит заглушками, `backend/app/main.py`
- Test: `tests/test_contract_layer.py` — импорт, OpenAPI строится, словарь согласован со схемами

**Даёт:** все имена и типы, которые используют задачи 2–5. Задачи 2–5 читают эти файлы как источник истины.

- [ ] Написать файлы контрактного слоя.
- [ ] `python -c "from app.main import create_app; create_app().openapi()"` проходит.
- [ ] `pytest tests/test_contract_layer.py` проходит.
- [ ] Коммит `feat: слой контрактов каркаса — словари, схемы C1/C2, модели БД, безопасность`.

### Задача 2: ML-сервис C1 на заглушках

**Файлы:**
- Create: `ml/src/mkl/product_stub.py`, `ml/src/mkl/product_api.py`, `ml/tests/test_product_api.py`, `ml/Dockerfile`, `ml/.dockerignore`, `ml/requirements.lock`

**Использует:** `mkl.product_contract` (задача 1), `mkl.contract.make_alert_id` и `make_case_key`, `configs/heads.yaml` (бюджеты и горизонты).

**Даёт:**
- `mkl.product_api:app` на порту 8001 с эндпоинтами из спецификации;
- `product_stub.score(req: ScoreRequest) -> ScoreResponse`;
- `product_stub.weekly(asof: date) -> WeeklyResponse`;
- `product_stub.outcomes(items: list[OutcomeQuery]) -> list[OutcomeResult]`;
- `product_stub.ready(asof: date | None) -> ReadyResponse`;
- `product_stub.directions() -> list[DirectionItem]`.

- [ ] Тест (падает): `TestClient(app)` в stub-режиме:
  - `/health` → `mode == "stub"`;
  - `POST /api/v1/score {"asof": "2026-06-15"}` → 200, `ScoreResponse.model_validate` проходит, `heads` содержит A_link и D, алертов A_link в бюджете ≤ 20, D ≤ 3, у всех `source == "stub"`;
  - повтор с тем же `asof` даёт идентичный JSON;
  - канал из `issued_histories.A_link` с `sent_day = asof − 3` не попадает в бюджет;
  - `asof=2026-06-01` → все головы `no_data`, `alerts == []`;
  - `GET /api/v1/guard-weekly-inspections?asof=2026-06-15` (понедельник) → 200 и валидно, `?asof=2026-06-16` → 422, `2026-06-01` → `result_status == "no_data"`;
  - `POST /api/v1/outcomes` → по одному результату на запрос, значения из `hit/miss/unknown`;
  - `/ready?asof=2026-06-01` → `missing_data`;
  - при `ML_MODE=real` `POST /score` → 501, `detail` содержит `ML1-03`.
- [ ] Реализовать генератор и приложение. Тяжёлые модули `mkl` не импортируются в stub-режиме.
- [ ] `pytest ml/tests/test_product_api.py -q` — PASS; `pytest ml/tests -q --deselect tests/test_address.py --deselect tests/test_config.py::test_paths_exist --deselect tests/test_db.py` — без новых падений.
- [ ] `ml/Dockerfile` собирается из корня (`docker build -f ml/Dockerfile -t mkl-ml .`; исключения — `ml/Dockerfile.dockerignore`), контейнер отвечает на `/health`.
- [ ] Коммит `feat(ml): сервис C1 на заглушках для каркаса`.

### Задача 3: сервисы, миграция, seed и тесты backend

**Файлы:**
- Modify: `backend/app/services/*.py` — `NotImplementedError` из задачи 1 заменяется заглушками или живым минимумом по таблицам спецификации
- Create: `backend/app/audit.py`, `backend/app/seed.py`, `backend/alembic.ini`, `backend/app/migrations/env.py`, `backend/app/migrations/versions/0001_initial.py`
- Create: `tests/conftest.py`, `tests/test_auth.py`, `tests/test_endpoints_shape.py`, `tests/test_daily_run.py`, `tests/test_audit.py`
- Delete: `backend/app/model.py`, `backend/app/database.py`, `backend/app/cli.py`, `backend/app/static/*`, `tests/test_api.py`, `tests/test_model.py`, `tests/test_database.py`, `tests/test_data_contract.py`, `tests/test_readiness.py`

**Использует:** схемы и сигнатуры задачи 1, фикстуру `contracts/fixtures/ml_score_2026-06-15.json` (её пишет задача 2; до этого — `product_stub.score` напрямую не импортировать: backend не зависит от пакета `mkl`).

**Даёт:** рабочий `create_app()`, `alembic upgrade head` на Postgres и SQLite, `python -m app.seed`, `python -m app.services.daily_run --asof`.

- [ ] Тесты (падают):
  - вход `dispatcher` / `DEMO_PASSWORD` → 200 и cookie; неверный пароль → 401; `/me` без cookie → 401;
  - `PUT /settings` под `dispatcher` → 403, под `admin` при `DEMO_SETTINGS_LOCKED=1` → 403 `settings_locked`;
  - каждый GET из C2 под `admin` → 200 и валиден по своей схеме (параметризованный тест по списку путей);
  - `run_daily` с `ml_client`, подменённым на ответ фикстуры, создаёт `forecast_runs` и `forecasts`; повтор не создаёт дублей, но добавляет `forecast_versions`;
  - POST решения пишет строку `audit_log`.
- [ ] Реализовать.
- [ ] `pytest -q` — PASS; `ruff check backend tests` — чисто.
- [ ] Коммит `feat(backend): сервисы-заглушки, миграция, seed и тесты каркаса`.

### Задача 4: фронт-оболочка

**Файлы:**
- Create: `frontend/package.json`, `package-lock.json`, `tsconfig.json`, `vite.config.ts`, `index.html`, `src/main.tsx`, `src/App.tsx`
- Create: `src/api/client.ts`, `src/api/schema.d.ts` (генерируется), `src/auth/AuthContext.tsx`, `src/stream/useStream.ts`
- Create: `src/components/Layout.tsx`, `src/components/StateView.tsx`, `src/pages/*.tsx` (9 страниц), `src/styles.css`

**Использует:** `contracts/api_v1.openapi.json` (готов после задачи 1), `contracts/vocabularies.json`.

**Даёт:** `npm run gen:api`, `npm run build` → `frontend/dist`; `npm run dev` проксирует `/api` на `:8000`.

- [ ] `npm run gen:api && npx tsc --noEmit && npm run build` — без ошибок.
- [ ] Коммит `feat(frontend): оболочка на React с экранами-заглушками`.

### Задача 5: инфраструктура, скрипты, документы

**Файлы:**
- Create/Modify: `compose.yaml`, `compose.real.yaml`, `compose.stand.yaml`, `Dockerfile`, `.dockerignore`, `Makefile`, `.github/workflows/ci.yml`
- Create: `scripts/export_contracts.py`, `scripts/smoke_compose.py` (заменяет старый), `scripts/preload_demo.py`, `scripts/replay.py`, `scripts/emulate_ods.py`, `scripts/emulate_helpdesk.py`, `scripts/fetch_bundle.sh`, `scripts/fetch_bundle.ps1`, `scripts/load_test/locustfile.py`
- Modify: `README.md`, `docs/architecture.md`
- Create: `docs/OWNERSHIP.md`, `backend/entrypoint.sh`

**Использует:** всё выше.

- [ ] `docker compose config` — валиден.
- [ ] `python scripts/export_contracts.py` пересобирает `contracts/`.
- [ ] Коммит `chore: compose, Dockerfile, CI, скрипты-заглушки и документы каркаса`.

### Задача 6: сквозная проверка (координатор)

- [ ] `docker compose up -d --build`, затем `python scripts/smoke_compose.py` — все 8 шагов спецификации зелёные; `docker compose down -v`.
- [ ] `git status` чистый; `git rev-list --all --objects | grep -E '\.(csv|parquet|pkl|7z)$'` возвращает только разрешённое (пусто).
- [ ] В плане команды поправить строку про график работ: заказчик прислал ППР и ТО, их использует `f70674e`.
- [ ] Итоговый отчёт пользователю: что живое, что заглушка, как запустить, что дальше делает каждый.
