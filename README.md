# Москоллектор — сервис диспетчера

Хакатон ЛЦТ-2026, задача №8: прогноз отказов и рисков в инженерных коллекторах и рабочее
место диспетчера. Что делает команда до сдачи 29.09 и почему — в
[плане команды](docs/superpowers/plans/2026-09-25-team-plan-to-submission.md).

**Статус: рабочий продуктовый контур.** Backend хранит прогнозы, события, решения,
исходы, заявки, уведомления и аудит; поддерживает реальный импорт справочников и
идемпотентную загрузку СМВУ. Без бандла сервис запускается с ML-заглушкой и синтетическим
справочником. Ограничения ML-сценариев описаны в `ml/reports/ML_PRODUCTION_STATUS.md`.

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
<http://localhost:8000/docs>. В Swagger сначала выполните `POST /api/v1/auth/login`:
ответ установит HttpOnly cookie, которую браузер будет отправлять автоматически. Токен
в JSON и ручная подстановка JWT не требуются. Машинные клиенты используют `X-API-Key`.

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

Нужен бандл C4 (раздел 4 плана): 11 файлов, которые читают ML-сервис в режиме `real`,
дообучение и `scripts/replay.py`. Это модель A_link, фичестор `sensor` и `object`, v2-кэш
охранной очереди, справочник каналов, метки для `/outcomes`, события 2026 года,
`configs/features.yaml` и `reports/intrusion_eventtime_v2_build.json`. Список с местом
чтения каждого файла — словарь `REQUIRED` в `ml/scripts/build_bundle.py`. Бандл
распространяется архивом `bundle-YYYYMMDD-N.7z` под паролем датасета организаторов
(решение D10), ссылка — в поле «Доп. материалы» формы сдачи.

**1. Получить и проверить бандл.**

```bash
sh scripts/fetch_bundle.sh ~/Downloads/bundle-20260928-1.7z ./bundle
```

```powershell
powershell -File scripts\fetch_bundle.ps1 -Version $HOME\Downloads\bundle-20260928-1.7z -Dest .\bundle
```

Вместо пути к `.7z` можно передать URL, распакованный каталог (скрипт его только проверит)
или версию `bundle-YYYYMMDD-N`: тогда скрипт ищет `./<версия>.7z`, а если его нет, скачивает
`$BUNDLE_URL/<версия>.7z`. Нужен 7z (`7z`, `7za` или `7zz`); пароль он спросит сам, без
терминала возьмёт из `BUNDLE_PASSWORD`. Скрипт распаковывает в `<каталог>.partial`,
сверяет `MANIFEST.sha256` (в sh — `sha256sum -c`) и раскладку томов
`compose.real.yaml`, переносит результат в `<каталог>` и печатает строку `BUNDLE_DIR=…`.
Впишите её в `.env`; без неё compose берёт `./bundle`.

**2. Запустить.**

```bash
docker compose -f compose.yaml -f compose.real.yaml up -d --build
```

Real-режим требует совместимый бандл моделей и признаков; без него используйте
`ML_MODE=stub`. Backend всегда сохраняет явные состояния `no_data`, `stale`,
`empty_valid` и `error`, не подменяя их прошлой успешной выдачей.

Стенд (PM-10) добавляет третий файл, `compose.stand.yaml`: Caddy с TLS на портах 80 и 443
для домена из `STAND_DOMAIN`, порт 8000 наружу закрыт, `DEMO_SETTINGS_LOCKED=1`,
`COOKIE_SECURE=1`. Для `!reset` в этом файле нужен Compose v2.24 или новее.

### Сборка бандла из датасета

Нужны датасет организаторов и ML-окружение (`make install-ml` или строка «Установка ML» в
разделе «Режим разработки»). Команды шагов 2–5 выполняются из `ml/`; `python` в них —
интерпретатор `.venv/bin/python` (Windows: `.venv\Scripts\python`).

1. Журналы `ext-journal-YYYY.csv` положить в `ml/data/raw/`, справочники
   `справочник_каналов_датчиков.csv` и `справочник_объектов_диспетчер.csv` — в
   `ml/Materials/`.
2. `python -m mkl.cli run` — приём, эпизоды, суточная панель, погода, фичестор, обучение
   A_link. Стадии перечислены в `ml/src/mkl/pipeline.py`; погода качается из сети,
   без сети её колонки остаются пустыми.
3. `python scripts/build_intrusion_eventtime_labels.py` — v2-кэш охранной очереди.
4. Необязательно: `python scripts/normalize_maintenance_schedules.py --ppr <ППР.xlsx>
   --to <ТО.xlsx> --available-from YYYY-MM-DD` — графики ППР и ТО для контекста алертов
   A_link. Без них алерты идут со статусом контекста `schedule_not_loaded`.
5. `python scripts/build_bundle.py --out ../dist --version bundle-YYYYMMDD-N --archive`
   копирует нужные файлы в `dist/<версия>/`, пишет `MANIFEST.sha256` и упаковывает всё
   в `dist/<версия>.7z` с шифрованием имён файлов. Пароль 7z спросит сам или возьмёт из
   `BUNDLE_PASSWORD`. Если обязательного файла нет, скрипт перечисляет все недостающие и
   ничего не копирует. `--dry-run` только проверяет состав и печатает размеры.
6. Из корня репозитория проверить архив тем же путём, что пройдёт эксперт:
   `sh scripts/fetch_bundle.sh dist/<версия>.7z ./bundle`.

Время полной сборки шагов 1–5 из исходного 7z-архива датасета: **не замерено** (ML2-12,
прогон на отдельной Linux-ВМ).

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
| Тесты ML | `make test-ml` | `cd ml; .venv\Scripts\python -m pytest -q --deselect tests/test_address.py --deselect tests/test_config.py::test_paths_exist --deselect tests/test_db.py` |
| Линтер | `make lint` | `.venv\Scripts\ruff check backend tests scripts` |
| Контракты | `make contracts` | `.venv\Scripts\python scripts\export_contracts.py; cd frontend; npm run gen:api` |
| Compose | `make up`, `make smoke`, `make down` | `docker compose up -d --build`, `python scripts\smoke_compose.py`, `docker compose down` |

Три теста ML исключены: им нужны данные заказчика, которых нет ни в git, ни в CI.

## Раскладка репозитория

```
backend/app/         API C2: config, db, models, security, audit, main
  routers/           маршруты по разделам C2, только права и параметры
  schemas/           pydantic-модели C2 и зеркало C1 (ml.py)
  services/          прикладная логика; сигнатуры — services/signatures.md
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

Поменяли схему, словарь или контрактный ответ — перегенерируйте и закоммитьте результат в
том же PR. CI (job `contracts` и шаг `gen:api` в job `frontend`) падает, если после
пересборки `git diff` не пуст. Правила изменения — в [CONTRIBUTING.md](CONTRIBUTING.md),
раздел «Каркас и контракты».

## Кто за что отвечает

Что каждый делает до сдачи, к какому сроку и как понять, что готово: [docs/MVP_TASKS.md](docs/MVP_TASKS.md).

Путь → роль → ID задачи плана → контракт: [docs/OWNERSHIP.md](docs/OWNERSHIP.md). Правка
в чужом файле — через PR с ревью владельца.
