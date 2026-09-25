# Каркас продукта «Москоллектор»: дизайн

Дата: 25.09.2026. Основа — план команды `docs/superpowers/plans/2026-09-25-team-plan-to-submission.md`. Имена и форматы контрактов C1–C5, словари C3 и ID задач взяты из него.

## Цель

Одна ветка `chore/skeleton`, в которой продукт поднимается целиком командой `docker compose up --build` на машине без данных заказчика: вход, журнал прогнозов, все экраны, все эндпоинты. Каждый участник дописывает свои заглушки, не трогая чужие файлы и не ломая соседей.

## Что сделано до спецификации

Worktree `U:\hackathon-skeleton`, ветка `chore/skeleton` от `origin/fix/data-contract-and-readiness` (PR #2). Уже закоммичено:

1. Корневой `.gitignore` закрывает `Materials/`, `ChatExport*`, `models/`, `data/`, `*.parquet`, `*.pkl`, `*.7z`, `*.duckdb`, `.env`, `frontend/node_modules`, `frontend/dist`.
2. `git subtree add --prefix=ml origin/feature/catalog-aware-ml` на коммите `f70674e` от 25.09 20:38.
3. `ml/.gitignore` закрывает весь `ml/data`, `ml/models` и `ml/Materials`.

## Три уровня готовности кода

| Уровень | Что это | Где |
|---|---|---|
| **Живое** | Работает по-настоящему: без него каркас не запускается или не проверяется | монорепо, compose, Dockerfile'ы, схема БД и миграция, словари, вход и роли, аудит изменяющих запросов, SSE-брокер, сквозная труба прогнозов, экспорт контрактов, CI |
| **Заглушка** | Функция с настоящей сигнатурой. Возвращает ответ, валидный по контракту, из детерминированных синтетических данных. В шапке указаны владелец, ID задачи, контракт и что заменить | все остальные сервисы бэкенда, все эндпоинты C1 кроме `/health`, скрипты, экраны фронта |
| **Чужой код** | Не меняется | всё в `ml/`, кроме новых файлов `product_*.py`, `Dockerfile`, `.dockerignore` и новых тестов |

**Формат шапки заглушки.** Один и тот же в Python и TS:

```
ЗАГЛУШКА — владелец ML2-01 (C5).
Заменить: правила классификации по типу датчика, справочник состояний, склейку «число + текст».
Контракт: вход и выход не меняются; тест test_semantics_shape.py должен остаться зелёным.
```

Синтетические данные нигде не выдаются за настоящие: объекты называются «Объект-заглушка N», в ответах `source="stub"`.

## Раскладка

```
.gitignore                     (готово)
README.md                      обзор, запуск, раскладка, ссылка на OWNERSHIP
compose.yaml                   db (postgres:16), ml (ML_MODE=stub по умолчанию), api
compose.real.yaml              override: тома бандла C4 и ML_MODE=real (для ML-инженеров и стенда)
compose.stand.yaml             override-заглушка для PM-10: Caddy, TLS, DEMO_SETTINGS_LOCKED=1
Dockerfile                     api: стадия node собирает frontend/ → backend/app/static; стадия python
.dockerignore                  + frontend/node_modules, ml/, data/, Materials/
pyproject.toml                 backend (зависимости ниже)
backend/alembic.ini
backend/app/
  main.py                      create_app(): роутеры, /api/v1, раздача static, lifespan
  config.py                    Settings из env
  db.py                        engine, Session, get_db
  models.py                    ORM: все таблицы BE-02
  migrations/                  Alembic: env.py, versions/0001_initial.py
  vocab.py                     чтение contracts/vocabularies.json
  security.py                  хеш пароля, подписанная cookie-сессия, require_perm(), X-API-Key
  audit.py                     middleware: изменяющие запросы и входы → audit_log
  schemas/                     pydantic-модели C2 (по файлу на раздел)
  routers/                     по файлу на раздел C2, только маршруты и зависимости
  services/                    логика; заглушки живут здесь
    ml_client.py               живое: HTTP-клиент C1 + pydantic-зеркало ответа
    daily_run.py               живое (минимум): вызов /score → upsert прогнозов; правила issued_log — PM-09
    forecasts.py               живое (минимум): список и карточка из БД; фильтры и группировка — BE-05
    semantics.py               заглушка ML2-01
    events.py, ingest.py       заглушки ML2-03
    decisions.py, work_orders.py   заглушки BE-06
    dashboard.py, quality.py, reference.py, system.py   заглушки BE-05 / BE-03
    geo.py                     заглушка ML1-11
    notifications.py           живое: in-process брокер SSE; события и уведомления — BE-08
    export.py                  заглушка BE-11
    settings_store.py          заглушка BE-05
  seed.py                      демо-пользователи из env, reason_codes из словаря, синтетический справочник
  static/                      пусто в git (сюда кладётся сборка фронта)
tests/                         тесты backend
ml/                            код CAML (не менять) +
  src/mkl/product_contract.py  pydantic-модели C1
  src/mkl/product_stub.py      синтетический генератор ответов C1
  src/mkl/product_api.py       FastAPI-приложение C1: ML_MODE=stub|real
  tests/test_product_api.py
  Dockerfile, .dockerignore
frontend/                      React + Vite + TypeScript
contracts/
  vocabularies.json            словари C3 и матрица прав
  ml_v1.schema.json            экспорт из product_contract.py
  api_v1.openapi.json          экспорт из backend
  fixtures/*.json              экспорт ответов заглушек
  README.md                    правила изменения контрактов
scripts/
  export_contracts.py          живое: пересобирает всё содержимое contracts/
  smoke_compose.py             живое: сквозная проверка на поднятом compose
  preload_demo.py              заглушка PM-09
  replay.py                    заглушка ML2-03
  emulate_ods.py, emulate_helpdesk.py   заглушки ML2-11
  fetch_bundle.sh, fetch_bundle.ps1     заглушки ML2-02
  load_test/locustfile.py      заглушка ML1-13
docs/
  OWNERSHIP.md                 путь → роль → ID задачи
  superpowers/plans/2026-09-25-team-plan-to-submission.md   копия плана команды
  superpowers/specs/2026-09-25-skeleton-design.md           этот файл
.github/workflows/ci.yml       backend, ml, frontend, contracts, smoke
```

**Удаляется старый MVP `main`:**
- `backend/app/model.py`, `database.py`, `cli.py`;
- старый UI в `backend/app/static/`;
- тесты старой схемы: `test_api.py`, `test_model.py`, `test_database.py`, `test_data_contract.py`, `test_readiness.py`;
- старый `scripts/smoke_compose.py`.

Всё это остаётся в истории git. Идеи PR #2 — разбор `t/f`, готовность, смоук compose — переходят в новый код. `analysis/` и `docs/review-resolution.md` остаются.

## Компоненты

### ML-сервис: `ml/src/mkl/product_api.py`

Отдельное FastAPI-приложение, реализует C1. Запуск: `uvicorn mkl.product_api:app --host 0.0.0.0 --port 8001`.

- **`ML_MODE=stub`** (по умолчанию). Все ответы строит `product_stub.py`: детерминированно по `asof`, валидно по `product_contract.py`, с `source="stub"`. Тяжёлые модули `mkl.service`, `serve`, `train` при импорте не загружаются, поэтому сервис стартует без данных.
- **`ML_MODE=real`**. Каждый эндпоинт вызывает функцию `_real_<name>()`. Сейчас она бросает `NotImplementedError` с ID задачи; ML-1 заменяет её вызовом `mkl.service`.

| Эндпоинт | Заглушка отдаёт | Владелец real-режима |
|---|---|---|
| `GET /health` | `{status, mode, schema_version}` (живое) | — |
| `GET /ready?asof=` | `ready`, для 2026-06-01 — `missing_data` | ML1-03 |
| `GET /api/v1/directions` | 3 пилотных сценария с `horizon_hours` и `budget_per_day` из `heads.yaml` | ML1-03 |
| `POST /api/v1/score` | Статусы голов A_link и D: `ok`, `empty_valid` или `no_data`. До 20 алертов A_link и 3 D в бюджете, `coverage`, `work_orders`. `alert_id` и `case_key` считаются формулами `contract.make_alert_id` и `make_case_key` — это лёгкий модуль без тяжёлых импортов. Паузы из `issued_histories` соблюдаются, чтобы PM-09 мог проверить правило журнала уже на заглушке | ML1-03, ML1-04, ML1-05b |
| `GET /api/v1/guard-weekly-inspections?asof=` | Понедельник → 0–2 рекомендации; не понедельник → 422; 2026-06-01 → `no_data` | ML1-04 |
| `POST /api/v1/outcomes` | `hit`, `miss` или `unknown` детерминированно по id | ML1-07 |

`product_contract.py` — pydantic-модели:
- `ScoreRequest`, `ScoreResponse`, `HeadStatus`;
- `AlertOut` — поля `Alert.to_dict()` из `contract.py`, `Address` вложенный;
- `CoverageOut`, `WorkOrderOut` (+ `obj_name`, `obj_parent_name`);
- `WeeklyResponse`, `WeeklyPriority`;
- `OutcomeItem`, `OutcomeResult`;
- `ReadyResponse`, `DirectionsItem`.

Схема экспортируется в `contracts/ml_v1.schema.json`.

`ml/Dockerfile`:
- база `python:3.12-slim` + `libgomp1`;
- `pip install -e .[api]` с версиями по `.venv` (xgboost 3.4.1, scikit-learn 1.9.1, lightgbm 4.7.0, catboost 1.2.10, polars 1.44.2, numpy 2.5.3, duckdb 1.5.5, pyarrow 25.0.1, pandas 3.0.5), пины в `ml/requirements.lock`;
- `MKL_ROOT=/srv/ml`, `EXPOSE 8001`.

Раскладку томов бандла для real-режима описывает `compose.real.yaml` по разделу 3 плана.

### Backend: `backend/app`

**Зависимости.**
- Основные: `fastapi`, `uvicorn[standard]`, `sqlalchemy>=2`, `psycopg[binary]>=3`, `alembic`, `pydantic-settings`, `itsdangerous`, `python-multipart`, `httpx`, `openpyxl`.
- Dev: `pytest`, `ruff`.
- `pandas`, `numpy`, `scikit-learn` из старого MVP убираются.

**Настройки (env).**

| Переменная | Значение по умолчанию / пример |
|---|---|
| `DATABASE_URL` | — |
| `ML_URL` | `http://ml:8001` |
| `DEMO_TODAY` | `2026-06-30` |
| `DEMO_SETTINGS_LOCKED` | `0` |
| `SECRET_KEY` | — |
| `DEMO_PASSWORD` | общий пароль демо-пользователей; обязателен |
| `INTEGRATION_API_KEY` | — |
| `SEED_DEMO` | `1` |
| `CONTRACTS_DIR` | — |

`.env.example` без реальных значений.

**Схема БД** — все таблицы BE-02 одной миграцией `0001_initial`. Типы переносимые (`JSON`, `DateTime(timezone=True)`), поэтому тесты идут на SQLite, а CI и compose — на PostgreSQL 16. Таблицы:

| Группа | Таблицы и поля |
|---|---|
| Пользователи | `users(login, name, role, password_hash)` |
| Справочники | `ref_objects(id, level, parent_id, kind, name)`; `ref_channels(id, obj_id, system, sensor_type, tag, name, picket)` |
| Прогнозы | `forecast_runs(id, asof, kind, started_at, finished_at, heads JSON, raw JSON)`; `forecasts(id PK, kind, scenario, head, asof, valid_from, valid_to, horizon_hours, score_type, risk, priority_score, rank, obj_id, channel_id, address JSON, factors JSON, data_status, case_key, source, first_run_id, last_run_id)`; `forecast_versions(forecast_id, run_id, risk, rank, payload JSON)`; `issued_log(head, asof, entity_key, forecast_id)` |
| Решения | `decisions(id, forecast_id, action, reason_code, comment, author, created_at, source)`; `reason_codes(code, title, actions JSON)`; `outcomes(forecast_id, outcome_auto, outcome_manual, comment, event_at, channel_id, author, updated_at, source)` |
| Заявки | `work_orders(id, forecast_ids JSON, scenario, obj_id, priority, work_type, due_by, status, created_by, created_at, source)`; `work_order_history(order_id, from_status, to_status, author, reason, at)` |
| События | `events(id, event_id, channel_id, ts, alarm, val_raw, val_num, event_class, hint, batch_id)` + уникальный индекс по нормализованному кортежу; `ingest_batches(id, kind, received_at, rows_total, accepted, duplicates, rejected, outside_demo_window, status)`; `ods_journal(id, ts, obj_id, record_type, decision, reason, batch_id)` |
| Служебные | `notifications(id, ts, kind, severity, payload JSON)`; `audit_log(id, ts, user, role, method, path, status, entity, payload JSON)`; `settings(key, value JSON)` |

**Вход и роли (живое).**
- `POST /auth/login` проверяет пароль и ставит HttpOnly cookie, подписанную `itsdangerous`, со сроком 12 ч.
- `require_perm(perm)` проверяет право роли по матрице из `vocabularies.json`.
- `X-API-Key` равный `INTEGRATION_API_KEY` даёт роль `integration`.
- Демо-пользователи — по одному на роль: `dispatcher`, `technician`, `analyst`, `manager`, `admin`. Пароль — `DEMO_PASSWORD`; в README его нет (D13).

**Аудит (живое, минимум).** Middleware пишет в `audit_log` каждый POST, PATCH, PUT и DELETE, а также GET на `/export` и `/audit`: пользователь, метод, путь, код ответа. Расширение — BE-09.

**Сквозная труба (живое, минимум).**
- `services/daily_run.run_daily(asof)`:
  - вызывает `ml_client.score()`, передавая в `issued_histories` журнал из `issued_log` за 7 дней до `asof`, без правил замены дня (их пишет PM-09);
  - пишет `forecast_runs`;
  - делает upsert `forecasts` по id и добавляет строку в `forecast_versions`;
  - публикует `alert.new` в брокер.
- Точки входа:
  - `POST /api/v1/admin/run-daily {asof}` (право `admin`);
  - `python -m app.services.daily_run --asof YYYY-MM-DD`.
- `GET /forecasts` и `GET /forecasts/{id}` читают из БД: пагинация и фильтр `scenario` работают; `decision`, `outcome`, `obj` и `group_by` принимаются, но пока не применяются — это BE-05. Поля без данных отдаются пустыми и валидными по схеме.

**Заглушки бэкенда.** Роутер вызывает функцию сервиса, функция возвращает pydantic-модель из `schemas/`. Сигнатуры окончательные.

| Функция | Что возвращает заглушка | Владелец |
|---|---|---|
| `dashboard.summary(db)` | KPI из счётчиков БД, диаграммы — синтетические ряды | BE-05 |
| `system.status(db, ml)` | `demo_today`, режим, статус ML из `/ready`, последний прогон (живое) | BE-05 |
| `decisions.create(db, forecast_id, body, user)` | Сохраняет решение без проверки причины по действию | BE-06 |
| `decisions.set_outcome(...)` | Сохраняет итог проверки | BE-06 |
| `work_orders.list/get/create/transition` | Список из БД. Переход без проверки графа статусов и без 409 | BE-06 |
| `events.list(db, filters)` | Страница синтетических событий | ML2-03 |
| `ingest.events(db, file_or_rows)` | Счётчики партии без записи | ML2-03 |
| `ingest.ods_journal(...)` | Счётчики партии без записи | BE-06 |
| `ingest.reset_day(...)` | Счётчики партии без записи | ML2-03 |
| `semantics.classify(sensor_type, val_raw, val_num, alarm)` → `(event_class, hint)` | `("alarm" if alarm else "normal", None)` | ML2-01 |
| `reference.tree(db)` | Дерево из синтетического справочника seed | BE-03 |
| `geo.schema_geojson(db, complex)` | `FeatureCollection` с 2 линиями и точками, `properties.note` | ML1-11 |
| `geo.schema_wkt(...)` | То же в WKT | ML1-11 |
| `quality.weekly(db, scenario)` | 4 синтетические недели | BE-05 |
| `export.forecasts_xlsx(db, period)` | XLSX с заголовками и без строк | BE-11 |
| `settings_store.get/put` | Настройки из таблицы `settings`; на `put` при `DEMO_SETTINGS_LOCKED=1` → 403 | BE-05 |

**SSE (живое).**
- `GET /api/v1/stream` отдаёт `text/event-stream` из in-process брокера. Heartbeat идёт каждые 15 с. `daily_run` публикует `alert.new`.
- `event.alarm`, `run.finished` и `workorder.changed` публикуют владельцы своих сервисов. Хранение уведомлений и `GET /notifications` — BE-08, сейчас это заглушка.

**Seed при старте.** Порядок: `alembic upgrade head` → `seed` → `uvicorn`. Seed создаёт пользователей, `reason_codes`, синтетический справочник (1 район → 2 комплекса → 6 объектов → 30 каналов с пикетами) и настройки по умолчанию. Прогнозы seed не создаёт: их создаёт `run-daily`, так проверяется труба.

### Frontend: `frontend/`

React + Vite + TypeScript. Версии закрепляет `package-lock.json`.

**Зависимости:**
- `react`, `react-dom`, `react-router-dom`, `openapi-fetch`;
- dev: `openapi-typescript`, `typescript`, `vite`, `@vitejs/plugin-react`.

Библиотеки диаграмм и карт в каркас не входят: их выбирают FE-02 и FE-07.

**Структура:**
- `src/api/schema.d.ts` — генерируется командой `npm run gen:api` из `contracts/api_v1.openapi.json`, коммитится;
- `src/api/client.ts` — `openapi-fetch` с `credentials: "include"`;
- `src/auth/AuthContext.tsx` — вход, `/me`, роль, выход (живое);
- `src/stream/useStream.ts` — подписка на SSE (живое);
- `src/components/Layout.tsx` (живое):
  - меню по правам роли;
  - плашка «Демо-время: 30.06.2026 — воспроизведение истории»;
  - статус ML и голов из `/system/status`;
  - колокольчик со счётчиком событий SSE;
- `src/components/StateView.tsx` — единый показ `loading`, `error`, `no_data`, `stale`, `empty_valid`, `threshold_infeasible` (живое, FE-10 дорабатывает);
- `src/pages/` — 9 страниц с шапкой владельца:

| Страница | Задача | Что умеет в каркасе |
|---|---|---|
| `Login` | FE-01 | живое |
| `Dashboard` | FE-02 | KPI из `/dashboard/summary` плоским списком |
| `Forecasts` | FE-03 | таблица `/forecasts` с пагинацией, ссылка на карточку |
| `ForecastCard` | FE-04 | поля карточки, форма решения (действие и причина из `/reason-codes`), отправка |
| `WorkOrders` | FE-05 | таблица `/work-orders` |
| `Events` | FE-06 | таблица `/events` и переключатель автообновления |
| `Schema` | FE-07 | число объектов из `/schema.geojson`, без карты |
| `Notifications` | FE-08 | лента событий SSE текущей сессии |
| `Quality` | FE-09 | таблица `/quality` |

**Разработка.**
- `npm run dev` на порту 5173 проксирует `/api` на `http://localhost:8000`.
- В Docker сборка идёт в стадии node корневого `Dockerfile`, результат копируется в `backend/app/static`.

### Контракты: `contracts/`

`scripts/export_contracts.py` пересобирает:
- `ml_v1.schema.json` — из `product_contract.py`;
- `api_v1.openapi.json` — из `create_app().openapi()`;
- фикстуры:
  - `ml_score_2026-06-15.json`, `ml_guard_weekly_2026-06-15.json`, `ml_outcomes.json` — из `product_stub`;
  - `api_forecasts.json`, `api_forecast_card.json`, `api_dashboard.json`, `api_events.json` — из заглушек бэкенда на тестовой БД.

CI запускает экспорт и падает, если `git diff --exit-code contracts/ frontend/src/api/schema.d.ts` не пуст. Правило: меняешь схему — перегенерируй и закоммить, тогда потребитель видит изменение в диффе PR.

`vocabularies.json` — словари C3 целиком:
- `scenario`, `kind`, `score_type`, `source` (`live`, `emulated`, `stub`);
- `action`, `reason_code` с допустимыми действиями, `outcome_manual`, `outcome_auto`;
- `result_status`, статусы заявки с разрешёнными переходами, приоритеты с соответствием русским значениям ML, `event_class`;
- `roles` и `permissions` (матрица прав).

Этот файл читают и `backend/app/vocab.py`, и фронт через `/reason-codes` и `/me`.

## Обработка ошибок

| Ситуация | Поведение |
|---|---|
| ML недоступен | `run_daily` пишет `forecast_runs` с головами в статусе `error` и `detail`; API отвечает 200, фронт показывает состояние через `StateView` |
| Ошибка валидации | 422 FastAPI |
| Нет прав | 403 |
| Не вошёл | 401 |
| Конфликт версий | 409 с `{detail, code}` |
| Заглушка ML в real-режиме без реализации | 501 с ID задачи в `detail` |
| Изменение настроек при `DEMO_SETTINGS_LOCKED=1` | 403 `settings_locked` |

## Тесты

- **Backend** (`pytest`, SQLite; в CI ещё раз на PostgreSQL-сервисе):
  - вход и права по матрице;
  - аудит пишет строку;
  - каждый эндпоинт C2 отвечает 200 или 201 по своей pydantic-схеме — тест формы, который владелец не должен ломать;
  - `run_daily` с подменённым `ml_client` на ответе фикстуры создаёт прогнозы, повторный прогон не создаёт дублей;
  - словарь грузится и согласован с перечислениями схем.
- **ML:**
  - `ml/tests/test_product_api.py` — все эндпоинты C1 в stub-режиме валидны по `product_contract`;
  - пауза из `issued_histories` соблюдается;
  - `2026-06-01` → `no_data`;
  - real-режим → 501.
  - Существующие тесты CAML прогоняются, без трёх, которым нужны данные: `test_address`, `test_config::test_paths_exist`, `test_db`.
- **Frontend:** `npm run gen:api` без диффа, `tsc --noEmit`, `vite build`.
- **Смоук** (`scripts/smoke_compose.py` на `docker compose up --build`):
  1. `/api/v1/system/status` = 200;
  2. вход под `dispatcher`;
  3. `POST /admin/run-daily` под `admin` на 2026-06-29;
  4. `/forecasts` не пуст;
  5. карточка открывается;
  6. решение сохраняется;
  7. `/` отдаёт `index.html`;
  8. `/stream` отдаёт первое событие.

## CI (`.github/workflows/ci.yml`)

Запускается на push в `main`, `chore/**`, `feat/**` и на все PR.

| Job | Что делает |
|---|---|
| `backend` | ruff + pytest, SQLite и Postgres 16 service |
| `ml` | `pip install -e ml[api,dev]`, `pytest ml/tests` с тремя исключениями |
| `frontend` | node 22: `npm ci`, `gen:api` без диффа, `tsc`, `build` |
| `contracts` | `export_contracts.py` без диффа |
| `smoke` | `docker compose up -d --build`, `smoke_compose.py`, `docker compose down -v` |

## Документы

- **`docs/OWNERSHIP.md`** — таблица «путь или файл → роль → ID задач плана → контракт». Плюс правило: в чужой файл — только через PR с ревью владельца.
- **`README.md`**:
  - что это и где план;
  - быстрый запуск: `cp .env.example .env`, вписать `DEMO_PASSWORD` и `SECRET_KEY`, `docker compose up --build`, открыть `http://localhost:8000`;
  - как запустить с реальными моделями (`compose.real.yaml` + бандл);
  - режим разработки фронта;
  - раскладка;
  - как перегенерировать контракты.
  
  Паролей в README нет.

## Сдача каркаса

- Коммиты только локальные в `chore/skeleton`.
- После вашего «да»: пуш ветки, PR в `main`, мёрж через «Create a merge commit». Перед мёржем — `git subtree pull --prefix=ml origin feature/catalog-aware-ml`, чтобы забрать свежие коммиты Александра.
- До мёржа участники могут ответвляться от `chore/skeleton`.

## Вне каркаса

- Реальный скоринг, модели, бандл, стенд.
- Логика любой заглушки.
- Библиотеки диаграмм и карт.
- Документация для сдачи (PM-12).
- Презентация.
