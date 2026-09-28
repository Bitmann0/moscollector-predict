# Образ api: FastAPI из backend/app и собранный фронт в backend/app/static.
# Контекст — корень репозитория; что в него не входит, перечислено в .dockerignore.
# У ML свой образ: ml/Dockerfile.

# --- стадия 1: сборка фронта --------------------------------------------------
FROM node:22-slim AS build
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Фронт ссылается на ../contracts (gen:api, словари): из /fe это /contracts.
COPY contracts/ /contracts/
RUN npm run build

# --- стадия 2: api ------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

# Ставятся только зависимости из pyproject.toml, сам пакет app — нет. Код запускается
# из /app/backend: config.ROOT = APP_DIR.parents[1] даёт /app, отсюда
# contracts_dir=/app/contracts и static_dir=/app/backend/app/static. Установленная
# в site-packages копия увела бы ROOT в /usr/local/lib. Отдельный слой зависимостей
# не пересобирается при правке кода.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY contracts/ /app/contracts/
# С кодом приходит шрифт DejaVu Sans для отчёта PDF (backend/app/resources/fonts), поэтому
# системные шрифты и apt-пакет fonts-dejavu-core образу не нужны.
COPY backend/ /app/backend/
COPY --from=build /fe/dist/ /app/backend/app/static/

# CRLF из рабочей копии Windows, сделанной до .gitattributes, ломает sh: срезаем \r.
RUN sed -i 's/\r$//' /app/backend/entrypoint.sh \
    && addgroup --system app && adduser --system --ingroup app app

USER app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

ENTRYPOINT ["sh", "/app/backend/entrypoint.sh"]
