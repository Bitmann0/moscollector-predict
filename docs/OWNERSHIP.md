# Владельцы кода

Кто отвечает за каждый путь репозитория. ID задач и контракты C1–C5 — из
[плана команды](superpowers/plans/2026-09-25-team-plan-to-submission.md), разделы 4 и 5.
Роли по никам GitHub фиксирует kickoff (PM-01): ML-1 — Александр Щербаков, PM — Денис;
BE, ML-2 и FE план называет предположительно, поэтому здесь указаны роли, а не люди.
Задачи и сроки по ролям простым языком — в [MVP_TASKS.md](MVP_TASKS.md).

## Правила

- **Чужой файл меняется только через PR с ревью владельца.** Ревьюеры по разделу 7 плана:
  `ml/` — ML-1 ↔ ML-2; клиент C1 и дневной цикл (PM-09) — ML-1; `backend/` — BE, в том числе
  для ML2-03 и PM-09; фронт — PM.
- **Контракты** (`contracts/`, `backend/app/schemas/`, `ml/src/mkl/product_contract.py`,
  `contracts/vocabularies.json`) меняются только через PR, в котором запущены
  `python scripts/export_contracts.py` и `npm run gen:api` и закоммичен результат. CI
  (job `contracts`, шаг `gen:api` в job `frontend`) падает на непустом диффе. После
  заморозки v1 в Сб 26.09 12:00 — только добавления; ломающее изменение идёт через PM с
  аппрувом обеих сторон.
- **Заглушку заменяет её владелец.** Шапка «ЗАГЛУШКА — владелец …» уходит вместе с
  заменой; тест формы, названный в шапке, остаётся зелёным. На 28.09 заглушки заменены:
  `git grep -l ЗАГЛУШКА -- ':!*.md'` на `main` (`b4af088`) ничего не находит. Остаётся
  `ML_MODE=stub` (`ml/src/mkl/product_stub.py`) и синтетический справочник
  `contracts/synthetic_reference.json` — только для CI и машин без данных заказчика;
  с бандлом ML работает в режиме `real`, а seed берёт настоящий справочник.

## ML — `ml/`

| Путь | Роль | ID задач | Контракт |
|---|---|---|---|
| `ml/src/mkl/product_contract.py` | ML-1 | ML1-03 | C1 (источник истины) |
| `ml/src/mkl/product_api.py`: `_real_ready`, `_real_directions`, `_real_score` | ML-1 | ML1-03, ML1-05b | C1 |
| `ml/src/mkl/product_api.py`: `_real_weekly`, устойчивость (замок, `no_data`) | ML-1 | ML1-04 | C1 |
| `ml/src/mkl/product_api.py`: `_real_outcomes` | ML-1 | ML1-07 | C1 |
| `ml/src/mkl/product_stub.py`, `contracts/fixtures/ml_*.json` | ML-1 | ML1-02 | C1 |
| `ml/tests/test_product_api.py` | ML-1 | ML1-03 | C1 |
| `ml/Dockerfile`, `ml/.dockerignore`, `ml/Dockerfile.dockerignore`, `ml/requirements.lock` | ML-2 | ML2-10 | раздел 3 плана |
| `ml/scripts/build_bundle.py` | ML-2 | ML2-02, ML2-12 | C4 |
| `ml/reports/SUBMISSION_METRICS.md` и `.json` | ML-1 | ML1-08 | D12 |
| `ml/scripts/train_latest.py`, модели окна | ML-1 | ML1-05a, ML1-05b, ML1-10 | C4 |
| остальное в `ml/` (код CAML) | ML-1 ↔ ML-2 | ML2-05, ML2-09 и др. | — |

## Backend — `backend/`, `tests/`

Сервисы — по [`backend/app/services/signatures.md`](../backend/app/services/signatures.md):
сигнатуры окончательные, тела меняет владелец.

| Путь | Роль | ID задач | Контракт |
|---|---|---|---|
| `backend/app/config.py`, `db.py`, `main.py`, `seed.py` | BE | BE-00 | — |
| `backend/app/models.py`, `backend/app/migrations/`, `backend/alembic.ini` | BE | BE-02 | схема БД |
| `backend/app/routers/*`, `backend/app/schemas/*` кроме `ml.py` | BE | BE-05 | C2 |
| `backend/app/schemas/ml.py`, `services/ml_client.py`, `services/daily_run.py` | PM | PM-09 | C1 (зеркало) |
| `backend/app/vocab.py` | BE | BE-05 | C3 |
| `backend/app/security.py`, `audit.py`, `services/audit_query.py` | BE | BE-09 | C3 (права) |
| `services/system.py`, `dashboard.py`, `forecasts.py`, `quality.py`, `settings_store.py` | BE | BE-05 | C2 |
| `services/decisions.py`, `work_orders.py`, `ingest.ingest_ods` | BE | BE-06 | C2, C3 |
| `services/reference.py`: `reason_codes`, `tree` | BE | BE-03 | C2 |
| `services/reference.py`: `sync` | BE | BE-14 | C2 |
| `services/events.py`, `ingest.py` (кроме `ingest_ods`) | ML-2 | ML2-03 | C5, C2 |
| `services/semantics.py` | ML-2 | ML2-01 | C5 |
| `services/geo.py` | ML-1 | ML1-11 | C2 |
| `services/notifications.py`: хранение и список (брокер SSE — живой, BE) | BE | BE-08 | C2 |
| `services/export.py` | BE | BE-11 | C2 |
| `services/helpers.py` | BE | — | — |
| `tests/` | владелец сервиса, который тест проверяет | — | тесты формы C2 |
| `backend/entrypoint.sh` | BE | BE-01 | — |

## Frontend — `frontend/`

| Путь | Роль | ID задач | Контракт |
|---|---|---|---|
| `src/pages/Login.tsx`, `src/auth/`, `src/components/Layout.tsx`, `src/stream/`, `src/api/client.ts` | FE | FE-01 | C2 |
| `src/api/schema.d.ts` | генерируется `npm run gen:api` | — | C2 |
| `src/pages/Dashboard.tsx` | FE | FE-02 | C2 |
| `src/pages/Forecasts.tsx` | FE | FE-03 | C2 |
| `src/pages/ForecastCard.tsx` | FE | FE-04 | C2, C3 |
| `src/pages/WorkOrders.tsx` | FE | FE-05 | C2, C3 |
| `src/pages/Events.tsx` | FE | FE-06 | C2, C5 |
| `src/pages/Schema.tsx` | FE | FE-07 | C2 |
| `src/pages/Notifications.tsx` | FE | FE-08 | C2 |
| `src/pages/Quality.tsx` | FE | FE-09 | C2 |
| `src/components/StateView.tsx` | FE | FE-10 | C1 `result_status` |

## Контракты — `contracts/`

| Путь | Роль | ID задач | Контракт |
|---|---|---|---|
| `vocabularies.json` | PM; аппрув BE и FE | PM-01 | C3 |
| `api_v1.openapi.json`, `fixtures/api_*.json` | BE; потребитель FE | BE-05, PM-08 | C2 |
| `ml_v1.schema.json`, `fixtures/ml_*.json` | ML-1; потребитель PM-09 | ML1-02, ML1-03 | C1 |
| `synthetic_reference.json` | PM | каркас | seed без настоящего справочника и `ML_MODE=stub` |

Файлы, кроме `vocabularies.json` и `synthetic_reference.json`, руками не правятся: их пишет
`scripts/export_contracts.py`.

## Скрипты — `scripts/`

Владелец указан в шапке каждого файла.

| Путь | Роль | ID задач | Контракт |
|---|---|---|---|
| `export_contracts.py` | PM | PM-05, PM-08 | C1, C2 |
| `smoke_compose.py`, `_api.py` | PM | PM-06 | C2 |
| `preload_demo.py` | PM | PM-09 | C1, C2 |
| `replay.py` | ML-2 | ML2-03 | C5 |
| `emulate_ods.py`, `emulate_helpdesk.py`, `_emulation.py` | ML-2 | ML2-11 | C2, C3 |
| `fetch_bundle.sh`, `fetch_bundle.ps1` | ML-2 | ML2-02 | C4 |
| `load_test/locustfile.py` | ML-1 | ML1-13 | C2 |

## Инфраструктура и документы

| Путь | Роль | ID задач | Контракт |
|---|---|---|---|
| `compose.yaml` | BE, PM | BE-01 | — |
| `compose.real.yaml` | ML-2 | ML2-10 | C4 (раскладка томов) |
| `compose.stand.yaml`, `deploy/Caddyfile` | PM | PM-10 | D9, D13 |
| `Dockerfile`, `.dockerignore` | BE; стадия node — FE | BE-01, FE-01 | — |
| `.github/workflows/ci.yml`, `Makefile`, `.gitattributes` | PM | PM-05, PM-06 | — |
| `README.md` | PM; раздел сборки — BE | PM-05, BE-13 | — |
| `docs/architecture.md` | BE | BE-13 | — |
| `docs/OWNERSHIP.md`, `docs/superpowers/` | PM | PM-01 | — |
| `analysis/` | ML-1 | ML1-12 | — |
