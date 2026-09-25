# Образ ML-сервиса C1 (mkl.product_api, порт 8001).
#
# Контекст сборки — корень репозитория: заглушке нужен
# contracts/synthetic_reference.json, а он лежит вне ml/.
#   docker build -f ml/Dockerfile -t mkl-ml .
# При -f ml/Dockerfile BuildKit берёт исключения из ml/Dockerfile.dockerignore,
# а не из корневого .dockerignore: данные и модели в образ не попадают.
#
# ML_MODE=stub (по умолчанию) стартует без данных. ML_MODE=real ждёт бандл C4
# томами в /srv/ml/data и /srv/ml/models (compose.real.yaml).
FROM python:3.12-slim

# libgomp1 — OpenMP для lightgbm, xgboost и catboost.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MKL_ROOT=/srv/ml \
    ML_MODE=stub \
    CONTRACTS_DIR=/srv/contracts

WORKDIR /srv/ml

# Зависимости отдельным слоем: он пересобирается только при смене пинов.
COPY ml/requirements.lock /srv/ml/requirements.lock
RUN pip install -r requirements.lock

COPY ml/ /srv/ml/
COPY contracts/synthetic_reference.json /srv/contracts/synthetic_reference.json
# --no-deps: версии уже закреплены requirements.lock, xgboost там в сборке без CUDA.
RUN pip install --no-deps -e .

EXPOSE 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4)"]

CMD ["uvicorn", "mkl.product_api:app", "--host", "0.0.0.0", "--port", "8001"]
