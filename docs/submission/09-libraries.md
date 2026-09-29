# Перечень библиотек и компонентов

Раздел отвечает на требование ТЗ §14: «Также должен быть предоставлен перечень всех
использованных библиотек и компонентов». Перечень снят 28.09.2026 на машине разработчика с образов api и
ml, собранных в тот же день в 00:59 МСК по `Dockerfile` и `ml/Dockerfile`, с lock-файлов
`frontend/package-lock.json` и `ml/requirements.lock` и с базовых образов из compose-файлов.
Пути даны от корня репозитория.

Изменения после снятия перечня, которые таблицы ниже уже учитывают:

- `pandas>=2.2` стал прямой зависимостью ML (`ml/pyproject.toml`, PR #34, температурный
  бэктест). Версия в `ml/requirements.lock` та же, 3.0.5: пакет перешёл из транзитивных в
  прямые, состав образа ml не изменился.
- backend получил прямые зависимости `fpdf2` (отчёт руководству в PDF, PR #55) и
  `defusedxml` (разбор XML при приёме, PR #56) и dev-зависимости `pypdf` и `xmlschema`.
  `defusedxml` до этого приходил в образ транзитивно через `fpdf2`. Версии `fpdf2` и его
  транзитивных пакетов сняты с образа api ветки PR #55, собранного 28.09.2026 в 21:26 МСК;
  версии `defusedxml`, `pypdf` и `xmlschema` — с venv разработчика.
- backend получил прямую зависимость `ldap3` и её транзитивную `pyasn1` (вход через LDAP/AD,
  #58); обе отмечены пометкой ³.

## Сводка

| Компонент | Где исполняется | Прямых | Транзитивных | Лицензии пакетов, попадающих в образ или сборку |
|---|---|---|---|---|
| backend | контейнер api | 13 и 4 dev | 26 | MIT — 18, BSD-3-Clause — 10, LGPL-3.0-only — 3, PSF-2.0 — 2, LGPL-3.0, Apache-2.0, BSD-2-Clause, MPL-2.0, MIT-CMU, «MIT или Apache-2.0» — по 1 |
| ML | контейнеры ml и replay | 12, 1 в группе `schedules` и 2 dev | 34 | MIT — 21, BSD-3-Clause — 15, Apache-2.0 — 3, ещё 7 — PSF-2.0, MIT-CMU, лицензия Matplotlib и составные (таблицы ниже) |
| frontend, браузерная сборка | браузер пользователя | 5 | 6 | MIT — 10, OFL-1.1 — 1 (шрифт) |
| frontend, инструменты сборки | стадия сборки образа api, `npm run dev` | 6 | 136 | MIT — 128, ISC — 7, Apache-2.0 — 3, BSD-3-Clause, Python-2.0, CC-BY-4.0, «MIT или CC0-1.0» — по 1 |
| базовые образы | Docker | 4 | — | PSF-2.0, MIT, PostgreSQL License, Apache-2.0 |

Для каждого стороннего пакета Python и npm лицензия определена. Не указана лицензия только у
собственного кода: см. раздел «Собственный код».

Копилефт и лицензии не для программного кода:

| Лицензия | Что под ней | Где находится |
|---|---|---|
| LGPL-3.0-only | `psycopg` и `psycopg-binary` 3.3.6, `fpdf2` 2.8.8 | образ api; ставятся из PyPI без изменений отдельными пакетами |
| LGPL-3.0 (в метаданных — «LGPL v3», без уточнения only или or-later) | `ldap3` 2.9.1 | образ api; ставится из PyPI без изменений отдельным пакетом |
| MPL-2.0 | `certifi` 2026.7.22 | образ api, транзитивно от `httpx` |
| GPL-3.0-or-later с GCC Runtime Library Exception 3.1 | системная библиотека `libgomp1` 14.2.0-19 | образ ml, ставится `apt-get` в `ml/Dockerfile` |
| OFL-1.1 | шрифт Golos Text, `@fontsource-variable/golos-text` 5.3.0 | файлы woff2 браузерной сборки |
| Bitstream Vera Fonts и Arev Fonts; правки DejaVu — общественное достояние | шрифт DejaVu Sans 2.35, обычный и полужирный; файлы взяты без изменений из `matplotlib` 3.11.2 (`mpl-data/fonts/ttf`), текст лицензии — `backend/app/resources/fonts/LICENSE` | образ api, `backend/app/resources/fonts/`; подмножество глифов встраивается в PDF отчёта |
| CC-BY-4.0 | `caniuse-lite` (база поддержки браузеров) | только инструменты сборки фронта, в браузерную сборку не попадает |
| LGPL-2.1-or-later, ограничение unRAR | 7-Zip | утилита хоста для упаковки и распаковки бандла |
| LGPL-2.1-or-later | `py7zr` 1.1.3 | только окружение аудита данных `analysis/` |
| GPL-2.0-only и другие | системные пакеты базовых образов; в Alpine, например, `busybox` и `apk-tools` | базовые образы |

## Как получен перечень

- **Python.** Внутри каждого образа запущен сбор метаданных через `importlib.metadata`.
  Лицензия берётся из поля `License-Expression`; если его нет — из короткого поля
  `License`. Пометка ¹ означает, что в поле `License` лежит полный текст лицензии или поле
  пустое, и SPDX-идентификатор определён по этому тексту или по файлу лицензии в
  `*.dist-info`. Пометка ² — лицензия определена по классификаторам `License ::` и файлам
  лицензии. Пометка ³ — пакет добавлен после снятия перечня (вход через LDAP/AD, #58); версия и лицензия взяты тем же способом из образа api, собранного 28.09 из
  ветки `feat/ldap-auth`. Для dev-пакетов, которых в образах нет, использованы venv разработчиков
  (Python 3.12.10, Windows).
- **Транзитивные зависимости Python** выведены из поля `Requires-Dist` установленных
  пакетов, маркеры окружения вычислены для Linux x86_64 и CPython 3.12. Из графа выводятся
  все пакеты образов, кроме `pip` 25.0.1 (MIT, приходит с базовым образом) и собственного
  пакета `mkl`.
- **npm.** Версии — из `frontend/package-lock.json` (формат lockfileVersion 3, 153 записи).
  Лицензия — поле `license` в `node_modules/<пакет>/package.json` установки по этому же
  lock-файлу на Windows x64: так проверены 103 пакета, у всех поле совпадает с полем
  `license` в lock-файле. У 50 платформенных сборок для других ОС, которые на Windows не
  ставятся, лицензия взята из lock-файла.
- **Образы.** Версии — из переменных окружения образов, `/etc/os-release` и журнала
  сборки BuildKit; лицензии — из файлов внутри образов, метки
  `org.opencontainers.image.licenses` или страницы проекта.

Проверить перечень на своей сборке можно так (имена образов compose — `moscollector-api`
и `moscollector-ml`):

```sh
docker run --rm --entrypoint python moscollector-api -c "from importlib.metadata import distributions as d; [print(x.metadata['Name'], x.version, x.metadata.get('License-Expression') or x.metadata.get('License'), sep=' | ') for x in d()]"
cd frontend && npm ci && npm ls --all --omit=dev
```

## Backend: образ api

Зависимости заданы диапазонами в `pyproject.toml`. Lock-файла у backend нет: `Dockerfile`
ставит их `pip install` при сборке, поэтому пересборка в другой день может взять более
новые версии внутри диапазонов. Версии ниже — из образа от 28.09.2026. В venv разработчика
стоят те же версии, кроме `pip` (26.2.1) и `uvloop`, которого нет:
`uvicorn[standard]` ставит его только вне Windows.

### Прямые зависимости

| Пакет | Требование | Версия | Лицензия | Назначение |
| --- | --- | --- | --- | --- |
| `fastapi` | `fastapi>=0.115,<1` | 0.141.1 | MIT | HTTP API C2 и схема OpenAPI (`backend/app/main.py`, `backend/app/routers/`) |
| `uvicorn` | `uvicorn[standard]>=0.30,<1` | 0.54.0 | BSD-3-Clause | ASGI-сервер api (`backend/entrypoint.sh`) |
| `SQLAlchemy` | `sqlalchemy>=2.0,<3` | 2.1.1 | MIT | ORM и запросы к БД (`backend/app/db.py`, `backend/app/models.py`) |
| `psycopg` | `psycopg[binary]>=3.2,<4` | 3.3.6 | LGPL-3.0-only | драйвер PostgreSQL (`postgresql+psycopg://` в `DATABASE_URL`, `compose.yaml`) |
| `alembic` | `alembic>=1.13,<2` | 1.20.0 | MIT | миграции схемы (`backend/app/migrations/`); `alembic upgrade head` при старте контейнера |
| `pydantic-settings` | `pydantic-settings>=2.4,<3` | 2.15.0 | MIT | настройки из окружения и `.env` (`backend/app/config.py`) |
| `itsdangerous` | `itsdangerous>=2.2,<3` | 2.2.0 | BSD-3-Clause¹ | подпись сессионной cookie (`URLSafeTimedSerializer`, `backend/app/security.py`) |
| `python-multipart` | `python-multipart>=0.0.9` | 0.0.32 | Apache-2.0 | разбор multipart/form-data для загрузки файлов (`UploadFile` в `backend/app/routers/ingest.py`) |
| `httpx` | `httpx>=0.27,<1` | 0.28.1 | BSD-3-Clause | HTTP-клиент к ML-сервису C1 (`backend/app/services/ml_client.py`) |
| `openpyxl` | `openpyxl>=3.1,<4` | 3.1.5 | MIT | выгрузка в XLSX (`backend/app/services/export.py`) и приём XLSX (`backend/app/services/ingest.py`) |
| `defusedxml` | `defusedxml>=0.7,<1` | 0.7.1 (venv) | PSF-2.0² | разбор XML-пачек приёма с запретом DTD, сущностей и внешних ссылок (`backend/app/xml_api.py`) |
| `fpdf2` | `fpdf2>=2.8,<3` | 2.8.8 | LGPL-3.0-only | отчёт руководству в PDF (`backend/app/services/report_pdf.py`) |
| `ldap3` | `ldap3>=2.9,<3` | 2.9.1³ | LGPL-3.0 | вход через каталог LDAP/AD (`backend/app/directory.py`) |
| `pytest` (dev) | `pytest>=8,<9` | 8.4.2 (venv; в образ не входит) | MIT | тесты `tests/` (`make test-backend`, CI) |
| `ruff` (dev) | `ruff>=0.6,<1` | 0.16.9 (venv; в образ не входит) | MIT | линтер (`make lint`, CI) |
| `pypdf` (dev) | `pypdf>=6,<7` | 6.19.0 (venv; в образ не входит) | BSD-3-Clause | текст PDF отчёта в `tests/test_report_pdf.py` |
| `xmlschema` (dev) | `xmlschema>=3,<5` | 4.3.2 (venv; в образ не входит) | MIT | проверка ответов XML по XSD в `tests/test_xml_api.py` |

### Транзитивные зависимости

| Пакет | Версия | Лицензия | Кем подтягивается |
| --- | --- | --- | --- |
| `annotated-doc` | 0.0.5 | MIT | fastapi |
| `annotated-types` | 0.8.0 | MIT | pydantic |
| `anyio` | 4.15.1 | MIT | httpx, starlette, watchfiles |
| `certifi` | 2026.7.22 | MPL-2.0 | httpcore, httpx |
| `click` | 8.5.0 | BSD-3-Clause | uvicorn |
| `et_xmlfile` | 2.0.0 | MIT | openpyxl |
| `fonttools` | 4.66.0 | MIT | fpdf2 |
| `h11` | 0.16.0 | MIT | httpcore, uvicorn |
| `httpcore` | 1.0.9 | BSD-3-Clause | httpx |
| `httptools` | 0.8.0 | MIT | uvicorn |
| `idna` | 3.20 | BSD-3-Clause | anyio, httpx |
| `Mako` | 1.4.3 | MIT | alembic |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause | Mako |
| `pillow` | 12.3.0 | MIT-CMU | fpdf2 |
| `psycopg-binary` | 3.3.6 | LGPL-3.0-only | psycopg |
| `pyasn1` | 0.6.4³ | BSD-2-Clause | ldap3 |
| `pydantic` | 2.13.5 | MIT | fastapi, pydantic-settings |
| `pydantic_core` | 2.46.5 | MIT | pydantic |
| `python-dotenv` | 1.2.3 | BSD-3-Clause | pydantic-settings, uvicorn |
| `PyYAML` | 6.0.3 | MIT | uvicorn |
| `starlette` | 1.7.0 | BSD-3-Clause | fastapi |
| `typing-inspection` | 0.4.4 | MIT | fastapi, pydantic, pydantic-settings |
| `typing_extensions` | 4.16.0 | PSF-2.0 | SQLAlchemy, alembic, anyio, fastapi, psycopg, pydantic, pydantic_core, starlette, typing-inspection |
| `uvloop` | 0.22.1 | MIT или Apache-2.0² | uvicorn |
| `watchfiles` | 1.3.0 | MIT | uvicorn |
| `websockets` | 17.1 | BSD-3-Clause | uvicorn |

## ML: образы ml и replay

Версии закреплены в `ml/requirements.lock`: 46 пинов, в образе ml стоят ровно эти 46
версий, а также `pip` и собственный пакет `mkl` 0.1.0 (`pip install --no-deps -e .`). Из
того же `ml/Dockerfile` собирается образ сервиса `replay` в `compose.stand.yaml`: ему нужен
`duckdb` для чтения parquet бандла.

Требование `xgboost>=3.0` из `ml/pyproject.toml` в образе закрывает пакет `xgboost-cpu`
3.4.1: модуль тот же (`import xgboost`), но без CUDA. По комментарию в
`ml/requirements.lock`, пакет `xgboost` на Linux тянет `nvidia-nccl-cu13` (290 МБ), а GPU в
контейнере нет. В venv разработчика ML стоит `xgboost` 3.4.1 с CUDA. Продуктовые модели
обучены LightGBM (`--backend lgbm` по умолчанию в `ml/scripts/train_latest.py`,
`ml/README.md`); XGBoost и CatBoost на GPU использовались в экспериментах, например в
ансамбле A_link (`ml/reports/A_LINK_ENSEMBLE.md`). Остальных пакетов этого venv, кроме
dev-зависимостей и их транзитивных, в `ml/pyproject.toml` нет, и в перечень они не включены.

Группа `schedules` (`openpyxl`) в lock-файл и образ не входит: она нужна только скрипту
подготовки графиков ППР и ТО.

### Прямые зависимости

| Пакет | Требование | Версия | Лицензия | Назначение |
| --- | --- | --- | --- | --- |
| `duckdb` | `duckdb>=1.0` | 1.5.5 | MIT¹ | SQL по parquet бандла: `ml/src/mkl/db.py`, признаки в `ml/src/mkl/features/`; поток событий `scripts/replay.py` |
| `polars` | `polars>=1.0` | 1.44.2 | MIT¹ | табличные преобразования панели, признаков и очередей (16 модулей `ml/src/mkl/`) |
| `numpy` | `numpy>=1.26` | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | численные массивы для моделей (`ml/src/mkl/train.py`, `ml/src/mkl/explain.py`) |
| `pandas` | `pandas>=2.2` | 3.0.5 | BSD-3-Clause¹ | таблицы температурного почасового бэктеста (`ml/src/mkl/temperature_episode.py`, `ml/scripts/exp_temperature_episode.py`); транзитивно его тянет и `catboost` |
| `scikit-learn` | `scikit-learn>=1.4` | 1.9.1 | BSD-3-Clause | изотоническая калибровка (`ml/src/mkl/calibrate.py`); PR-AUC, ROC-AUC, Брайер (`ml/src/mkl/metrics.py`) |
| `lightgbm` | `lightgbm>=4.5` | 4.7.0 | MIT | бустинг, бэкенд `MKL_BACKEND=lgbm` (`ml/src/mkl/train.py`) |
| `xgboost-cpu` | `xgboost>=3.0` | 3.4.1 | Apache-2.0 | бустинг, бэкенд по умолчанию в `mkl.train` (`MKL_BACKEND=xgb`); вклады признаков (`ml/src/mkl/explain.py`) |
| `catboost` | `catboost>=1.2` | 1.2.10 | Apache-2.0 | бустинг, бэкенд `MKL_BACKEND=cat`; вклады признаков (`ml/src/mkl/explain.py`) |
| `PyYAML` | `pyyaml>=6.0` | 6.0.3 | MIT | чтение `ml/configs/heads.yaml` (`ml/src/mkl/serve.py`) и реестра признаков `configs/features.yaml` из бандла (`ml/src/mkl/store.py`) |
| `pyarrow` | `pyarrow>=16.0` | 25.0.1 | Apache-2.0 | выдача результата DuckDB в Polars, `.pl()` (`ml/src/mkl/features/decay.py`) |
| `fastapi` (api) | `fastapi>=0.115` | 0.141.1 | MIT | HTTP API ML-сервиса C1 (`ml/src/mkl/product_api.py`) |
| `uvicorn` (api) | `uvicorn[standard]>=0.30` | 0.53.0 | BSD-3-Clause | ASGI-сервер ML-сервиса (CMD в `ml/Dockerfile`) |
| `openpyxl` (schedules) | `openpyxl>=3.1` | в образ не ставится (в образе api — 3.1.5) | MIT | разбор графиков ППР и ТО (`ml/scripts/normalize_maintenance_schedules.py`) |
| `pytest` (dev) | `pytest>=8.0` | 9.1.1 (venv; в образ не входит) | MIT | тесты `ml/tests/` |
| `httpx` (dev) | `httpx>=0.27` | 0.28.1 (venv; в образ не входит) | BSD-3-Clause | `TestClient` FastAPI в `ml/tests/test_api.py` |

### Транзитивные зависимости

| Пакет | Версия | Лицензия | Кем подтягивается |
| --- | --- | --- | --- |
| `annotated-doc` | 0.0.5 | MIT | fastapi |
| `annotated-types` | 0.8.0 | MIT | pydantic |
| `anyio` | 4.15.1 | MIT | starlette, watchfiles |
| `click` | 8.5.0 | BSD-3-Clause | uvicorn |
| `cloudpickle` | 3.1.2 | BSD-3-Clause | joblib |
| `contourpy` | 1.4.0 | BSD-3-Clause | matplotlib |
| `cycler` | 0.12.1 | BSD-3-Clause¹ | matplotlib |
| `fonttools` | 4.65.0 | MIT | matplotlib |
| `graphviz` | 0.21 | MIT | catboost |
| `h11` | 0.16.0 | MIT | uvicorn |
| `httptools` | 0.8.0 | MIT | uvicorn |
| `idna` | 3.20 | BSD-3-Clause | anyio |
| `joblib` | 1.6.0 | BSD-3-Clause | scikit-learn |
| `kiwisolver` | 1.5.1 | BSD-3-Clause¹ | matplotlib |
| `matplotlib` | 3.11.2 | лицензия Matplotlib (на основе PSF)¹ | catboost |
| `narwhals` | 2.26.0 | MIT | lightgbm, plotly, scikit-learn |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | matplotlib, plotly |
| `pillow` | 12.3.0 | MIT-CMU | matplotlib |
| `plotly` | 7.0.0 | MIT | catboost |
| `polars-runtime-32` | 1.44.2 | MIT | polars |
| `pydantic` | 2.13.5 | MIT | fastapi |
| `pydantic_core` | 2.46.5 | MIT | pydantic |
| `pyparsing` | 3.3.2 | MIT | matplotlib |
| `python-dateutil` | 2.9.0.post0 | Apache-2.0 и BSD-3-Clause (разные части кода)² | matplotlib, pandas |
| `python-dotenv` | 1.2.3 | BSD-3-Clause | uvicorn |
| `scipy` | 1.18.1 | BSD-3-Clause¹ | catboost, lightgbm, scikit-learn, xgboost-cpu |
| `six` | 1.17.0 | MIT | catboost, python-dateutil |
| `starlette` | 1.6.0 | BSD-3-Clause | fastapi |
| `threadpoolctl` | 3.6.0 | BSD-3-Clause | scikit-learn |
| `typing-inspection` | 0.4.4 | MIT | fastapi, pydantic |
| `typing_extensions` | 4.16.0 | PSF-2.0 | anyio, fastapi, pydantic, pydantic_core, starlette, typing-inspection |
| `uvloop` | 0.22.1 | MIT или Apache-2.0² | uvicorn |
| `watchfiles` | 1.2.0 | MIT | uvicorn |
| `websockets` | 17.1 | BSD-3-Clause | uvicorn |

`matplotlib`, `plotly` и `graphviz` попадают в образ только как зависимости `catboost`.

## Frontend

Код приложения в `frontend/src` импортирует только пакеты из `dependencies`; обращений к
внешним адресам в `frontend/src` и `frontend/index.html` нет. В журнале сборки образа api
от 28.09 на linux/amd64 `npm ci` установил 104 пакета, Vite 6.4.3 собрал 61 модуль, в
`dist/assets` легли четыре файла woff2 шрифта Golos Text. Локально сборка ложится в
`frontend/dist/`, в образе api — в `backend/app/static/`.

### Прямые зависимости

| Пакет | Требование | Версия | Лицензия | Назначение |
| --- | --- | --- | --- | --- |
| `@fontsource-variable/golos-text` | `^5.3.0` | 5.3.0 | OFL-1.1 | шрифт Golos Text; файлы woff2 входят в сборку (`frontend/src/main.tsx`) |
| `openapi-fetch` | `0.17.0` | 0.17.0 | MIT | типизированный HTTP-клиент к API по схеме OpenAPI (`frontend/src/api/client.ts`) |
| `react` | `18.3.1` | 18.3.1 | MIT | библиотека интерфейса |
| `react-dom` | `18.3.1` | 18.3.1 | MIT | отрисовка React в DOM браузера (`frontend/src/main.tsx`) |
| `react-router-dom` | `6.30.6` | 6.30.6 | MIT | маршрутизация между экранами (`frontend/src/App.tsx`) |
| `@types/react` (dev) | `18.3.31` | 18.3.31 | MIT | типы React для TypeScript |
| `@types/react-dom` (dev) | `18.3.7` | 18.3.7 | MIT | типы react-dom для TypeScript |
| `@vitejs/plugin-react` (dev) | `4.7.0` | 4.7.0 | MIT | JSX и Fast Refresh в Vite |
| `openapi-typescript` (dev) | `7.13.0` | 7.13.0 | MIT | генерация `frontend/src/api/schema.d.ts` из `contracts/api_v1.openapi.json` (`npm run gen:api`) |
| `typescript` (dev) | `5.9.3` | 5.9.3 | Apache-2.0 | проверка типов перед сборкой (`tsc --noEmit` в `npm run build`) |
| `vite` (dev) | `6.4.3` | 6.4.3 | MIT | сборка в `frontend/dist/` и dev-сервер с прокси `/api` (`frontend/vite.config.ts`) |

### Транзитивные зависимости, попадающие в браузерную сборку

Совпадают с выводом `npm ls --all --omit=dev`.

| Пакет | Версия | Лицензия |
| --- | --- | --- |
| `@remix-run/router` | 1.23.4 | MIT |
| `js-tokens` | 4.0.0 | MIT |
| `loose-envify` | 1.4.0 | MIT |
| `openapi-typescript-helpers` | 0.1.0 | MIT |
| `react-router` | 6.30.6 | MIT |
| `scheduler` | 0.23.2 | MIT |

### Все транзитивные зависимости: сводка по лицензиям

Платформенные сборки — бинарные пакеты под конкретные ОС и процессор: 26 пакетов
`@esbuild/*`, 25 пакетов `@rollup/*`, `@napi-rs/lzma-linux-x64-gnu` и `fsevents`. Из них
ставятся только подходящие платформе, остальные значатся в lock-файле для других ОС.

| Лицензия | Транзитивных | из них в браузерной сборке | из них платформенных сборок | Пакеты (кроме MIT) |
| --- | --- | --- | --- | --- |
| MIT | 129 | 6 | 53 |  |
| ISC | 7 | 0 | 0 | `electron-to-chromium`, `lru-cache`, `minimatch`, `picocolors`, `semver`, `yallist`, `yargs-parser` |
| Apache-2.0 | 2 | 0 | 0 | `baseline-browser-mapping`, `yaml-ast-parser` |
| Python-2.0 | 1 | 0 | 0 | `argparse` |
| CC-BY-4.0 | 1 | 0 | 0 | `caniuse-lite` |
| BSD-3-Clause | 1 | 0 | 0 | `source-map-js` |
| (MIT OR CC0-1.0) | 1 | 0 | 0 | `type-fest` |
| **итого** | 142 | 6 | 53 |  |

## Базовые образы и системные компоненты

| Образ | Где используется | Версия на 28.09 | Лицензия | Откуда версия и лицензия |
|---|---|---|---|---|
| `python:3.12-slim` | база образов api, ml и replay | Python 3.12.14, Debian 13 (trixie) | PSF-2.0 | `PYTHON_VERSION` и `/etc/os-release` в образе; `/usr/local/lib/python3.12/LICENSE.txt` |
| `node:22-slim` | только стадия сборки фронта в `Dockerfile`, в итоговый образ не входит | Node.js 22, npm 10.9.9; точная версия Node.js в журнале сборки не выводится | MIT | журнал сборки; файл `LICENSE` в репозитории nodejs/node, ветка v22.x |
| `postgres:16-alpine` | сервисы `db` (`compose.yaml`) и `backup` (`compose.stand.yaml`, `pg_dump`) | PostgreSQL 16.15, Alpine 3.24.2 | PostgreSQL License | `PG_VERSION` и `/etc/os-release` в образе; https://www.postgresql.org/about/licence/ |
| `caddy:2-alpine` | сервис `caddy` на стенде (`compose.stand.yaml`), TLS и обратный прокси | Caddy v2.11.4, Alpine 3.23.6 | Apache-2.0 | `CADDY_VERSION` и метка `org.opencontainers.image.licenses` образа |

Теги не закреплены дайджестом. Версии в таблице — у образов на машине разработчика; образы
postgres и caddy собраны 17.09.2026 (UTC). Новая загрузка тега может принести более новую
минорную версию. Дайджесты `python:3.12-slim` и `node:22-slim` взяты из записи сборки
образов api и ml 28.09, дайджесты `postgres:16-alpine` и `caddy:2-alpine` — у образов на
машине разработчика:

- `python:3.12-slim` — `sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`;
- `node:22-slim` — `sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c`;
- `postgres:16-alpine` — `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`;
- `caddy:2-alpine` — `sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b`.

Системные пакеты:

- образ api — 87 пакетов Debian, образ ml — 88: к базовым добавлен `libgomp1` 14.2.0-19,
  OpenMP для `lightgbm`, `xgboost` и `catboost`. Лицензия `libgomp1` — GPL-3.0-or-later с
  GCC Runtime Library Exception 3.1 (`/usr/share/doc/libgomp1/copyright` в образе);
  лицензии остальных пакетов Debian — в `/usr/share/doc/<пакет>/copyright`;
- `postgres:16-alpine` — 45 пакетов apk, `caddy:2-alpine` — 32; лицензия каждого
  выводится командой `apk list -I`. PostgreSQL в образе собран из исходников и в этот
  список не входит.

## Компоненты вне образов

| Компонент | Версия | Лицензия | Где и зачем | Источник лицензии |
|---|---|---|---|---|
| Swagger UI, `swagger-ui-dist@5` | мажорная версия 5 задана в FastAPI; на 28.09 последняя в npm — 5.33.0 | Apache-2.0 | страница `/docs` api: браузер загружает скрипт и стили с cdn.jsdelivr.net (умолчания `fastapi/openapi/docs.py`) | реестр npm |
| ReDoc, `redoc@2` | 2; на 28.09 последняя в npm — 2.5.4 | MIT | страница `/redoc` api: скрипт с того же CDN, шрифты с fonts.googleapis.com (`with_google_fonts=True` в `fastapi/openapi/docs.py`) | реестр npm |
| locust | не закреплена; на 28.09 последняя в PyPI — 2.46.6 | MIT | нагрузочный тест `scripts/load_test/locustfile.py`; в зависимости проекта не входит, ставится `pip install locust` | PyPI |
| 7-Zip (`7z`, `7za` или `7zz`) | не закреплена | LGPL-2.1-or-later; код LZFSE и ZSTD — BSD-3-Clause, XXH64 — BSD-2-Clause; распаковщик RAR — с ограничением unRAR | упаковка бандла `ml/scripts/build_bundle.py`, распаковка `scripts/fetch_bundle.sh` и `scripts/fetch_bundle.ps1` | https://www.7-zip.org/license.txt |
| `actions/checkout@v7`, `actions/setup-python@v7`, `actions/setup-node@v7` | v7 | MIT | CI, `.github/workflows/ci.yml` | лицензия репозиториев по GitHub API |

Если у браузера нет доступа к cdn.jsdelivr.net, страницы `/docs` и `/redoc` не
отрисуются (вывод из устройства страниц FastAPI, не проверялось); схема `/openapi.json`
отдаётся самим api.

Скрипты `scripts/*.py` импортируют только стандартную библиотеку Python. Исключения:
`scripts/replay.py` импортирует `duckdb` для parquet; `scripts/reclassify_events.py`
импортирует SQLAlchemy и `app.services` из backend, поэтому ему нужны зависимости из
`pyproject.toml`; `scripts/export_contracts.py` загружает приложение backend и ML-заглушку,
поэтому в CI перед ним выполняется `python -m pip install -e . pyyaml`. `scripts/fetch_bundle.sh`
вызывает также системные `curl` или `wget` и `sha256sum` или `shasum`; их лицензии не
проверялись: это утилиты ОС хоста.

Первичный аудит данных в `analysis/` выполнялся в отдельном окружении по
`analysis/requirements.txt`; в продукт оно не входит:

| Пакет | Версия | Лицензия | Источник лицензии |
|---|---|---|---|
| `duckdb` | 1.5.5 | MIT | та же версия в образе ml |
| `py7zr` | 1.1.3 | LGPL-2.1-or-later | PyPI |
| `pandas` | 3.0.1 | BSD-3-Clause | PyPI |
| `pypdf` | 6.10.0 | BSD-3-Clause | PyPI |
| `matplotlib` | 3.11.2 | лицензия Matplotlib (на основе PSF) | та же версия в образе ml |

## Собственный код

| Пакет | Версия | Манифест |
|---|---|---|
| `moscollector-predict` (backend) | 0.2.0 | `pyproject.toml` |
| `mkl` (ML) | 0.1.0 | `ml/pyproject.toml` |
| `moscollector-frontend` | 0.2.0 | `frontend/package.json`, `"private": true` |

Лицензия собственного кода не указана: файла `LICENSE` в репозитории нет, поля `license`
нет ни в одном из трёх манифестов.
