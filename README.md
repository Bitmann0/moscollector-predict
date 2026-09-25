# Москоллектор — сервис диспетчера

Хакатон ЛЦТ-2026, задача №8: прогноз отказов и рисков в инженерных коллекторах и рабочее
место диспетчера. Что делает команда до сдачи 29.09 и почему — в
[плане команды](docs/superpowers/plans/2026-09-25-team-plan-to-submission.md).

**Статус: каркас.** Продукт поднимается целиком на машине без данных заказчика, но
большая часть логики — заглушки. Что живое, а что заглушка, перечислено в
[спецификации каркаса](docs/superpowers/specs/2026-09-25-skeleton-design.md), раздел
«Три уровня готовности кода». У каждой заглушки в шапке — владелец, ID задачи и контракт.
Синтетические сущности называются «Объект-заглушка N», в ответах у них `source="stub"`.

## Быстрый запуск

Нужен Docker с Compose v2. Данные заказчика не нужны.

```bash
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
# вписать в .env DEMO_PASSWORD, SECRET_KEY, INTEGRATION_API_KEY
docker compose up --build
```

`SECRET_KEY` и `INTEGRATION_API_KEY` — любые длинные случайные строки, например
`python -c "import secrets; print(secrets.token_hex(32))"`.

Откройте <http://localhost:8000>. Пользователи — `dispatcher`, `technician`, `analyst`,
`manager`, `admin`, пароль у всех — `DEMO_PASSWORD` из `.env`. Swagger —
<http://localhost:8000/docs>.

После первого старта журнал прогнозов пуст: seed создаёт пользователей, справочник причин
решений и синтетический справочник объектов, но не прогнозы. Прогнозы даёт дневной прогон:

- `python scripts/preload_demo.py` — окно 2026-06-01…2026-06-29, по вызову на день;
- или один день — `POST /api/v1/admin/run-daily` с телом `{"asof": "2026-06-29"}` под
  `admin` (через Swagger).

Проверка контура целиком — `python scripts/smoke_compose.py`: вход, статус, прогон,
журнал, карточка, решение, фронт, поток; при сбое код выхода 1. `smoke_compose.py` и
`preload_demo.py` написаны на stdlib и запускаются без venv, нужен только Python 3.12.

Сервисы compose:

| Сервис | Что это | Порт |
|---|---|---|
| `db` | PostgreSQL 16, том `pgdata` | наружу не открыт |
| `ml` | ML-сервис C1, по умолчанию `ML_MODE=stub` | 8001, только внутри сети compose |
| `api` | API C2 и собранный фронт | 8000 |

`docker compose down` останавливает сервисы, `docker compose down -v` ещё и стирает БД.

## Запуск с реальными моделями

Нужен бандл C4 (раздел 4 плана): модели, фичестор, метки, v2-кэш охранной очереди,
`configs/features.yaml`, `reports/intrusion_eventtime_v2_build.json`. Как его получить,
печатает `scripts/fetch_bundle.sh` (или `.ps1`). Распакуйте бандл в `./bundle` или задайте
каталог в `BUNDLE_DIR` в `.env`.

```bash
docker compose -f compose.yaml -f compose.real.yaml up --build
```

Пока ML-1 не заменил функции `_real_*` в `ml/src/mkl/product_api.py`, эндпоинты ML в
real-режиме отвечают 501 с ID задачи в `detail`.

Стенд (PM-10) добавляет третий файл, `compose.stand.yaml`: Caddy с TLS на портах 80 и 443
для домена из `STAND_DOMAIN`, порт 8000 наружу закрыт, `DEMO_SETTINGS_LOCKED=1`,
`COOKIE_SECURE=1`. Для `!reset` в этом файле нужен Compose v2.24 или новее.

## Режим разработки

Python 3.12, Node 22. Для запуска без Docker добавьте в `.env` две строки — compose их
переопределяет, так что они не мешают `docker compose up`:

```
DATABASE_URL=sqlite:///./data/dev.db
ML_URL=http://localhost:8001
```

Команды выполняются из корня репозитория: backend читает `.env` из текущего каталога.

| Что | Unix (`make`) | Windows (PowerShell) |
|---|---|---|
| Установка backend | `make install` | `py -3.12 -m venv .venv; .venv\Scripts\pip install -e ".[dev]"` |
| Установка фронта | входит в `make install` | `cd frontend; npm ci` |
| Установка ML (тяжёлые зависимости) | `make install-ml` | `py -3.12 -m venv ml\.venv; ml\.venv\Scripts\pip install -r ml\requirements.lock; ml\.venv\Scripts\pip install --no-deps -e ml; ml\.venv\Scripts\pip install pytest httpx` |
| ML-заглушка на :8001 | `make dev-ml` | `$env:PYTHONPATH="ml\src"; .venv\Scripts\uvicorn mkl.product_api:app --port 8001` |
| api на :8000 | `make dev-api` | `.venv\Scripts\alembic -c backend\alembic.ini upgrade head; $env:PYTHONPATH="backend"; .venv\Scripts\python -m app.seed; .venv\Scripts\uvicorn app.main:app --app-dir backend --reload --port 8000` |
| Фронт на :5173, `/api` проксируется на :8000 | `make dev-fe` | `cd frontend; npm run dev` |
| Тесты backend | `make test-backend` | `.venv\Scripts\pytest -q` |
| Тесты ML | `make test-ml` | `cd ml; .venv\Scripts\pytest -q --deselect tests/test_address.py --deselect tests/test_config.py::test_paths_exist --deselect tests/test_db.py` |
| Линтер | `make lint` | `.venv\Scripts\ruff check backend tests scripts` |
| Контракты | `make contracts` | `.venv\Scripts\python scripts\export_contracts.py; cd frontend; npm run gen:api` |
| Compose | `make up`, `make smoke`, `make down` | `docker compose up -d --build`, `python scripts\smoke_compose.py`, `docker compose down` |

Три теста ML исключены: им нужны данные заказчика, которых нет ни в git, ни в CI.

## Раскладка репозитория

```
backend/app/         API C2: config, db, models, security, audit, main
  routers/           маршруты по разделам C2, только права и параметры
  schemas/           pydantic-модели C2 и зеркало C1 (ml.py)
  services/          логика; заглушки живут здесь, сигнатуры — services/signatures.md
  migrations/        Alembic, одна миграция 0001
  seed.py            пользователи, причины решений, синтетический справочник
backend/entrypoint.sh  старт контейнера: миграция, seed, uvicorn
tests/               тесты backend
frontend/            React + Vite + TypeScript; src/pages — 9 экранов
ml/                  ML-проект CAML целиком; продуктовый слой — src/mkl/product_*.py
contracts/           словари C3, схемы C1 и C2, фикстуры, синтетический справочник
scripts/             смоук, выгрузка контрактов, прелоад, проигрыватель, эмуляторы, нагрузка
deploy/Caddyfile     обратный прокси стенда
compose*.yaml        локальный запуск, реальные модели, стенд
docs/                план команды, спецификация каркаса, архитектура, владельцы
analysis/            аудит ТЗ и полного датасета
```

Схема сервисов и дневной цикл — в [docs/architecture.md](docs/architecture.md).

## Контракты

`contracts/` пересобирается из кода:

```bash
python scripts/export_contracts.py     # схемы C1 и C2 и фикстуры в contracts/fixtures/
cd frontend && npm run gen:api         # src/api/schema.d.ts из api_v1.openapi.json
```

Поменяли схему, словарь или ответ заглушки — перегенерируйте и закоммитьте результат в
том же PR. CI (job `contracts` и шаг `gen:api` в job `frontend`) падает, если после
пересборки `git diff` не пуст. Правила изменения — в [CONTRIBUTING.md](CONTRIBUTING.md),
раздел «Каркас и контракты».

## Кто за что отвечает

Путь → роль → ID задачи плана → контракт: [docs/OWNERSHIP.md](docs/OWNERSHIP.md). Правка
в чужом файле — через PR с ревью владельца.
