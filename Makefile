# Команды разработки (Unix). Эквиваленты для Windows — в README, раздел «Режим разработки».
.PHONY: install install-ml test test-backend test-ml lint contracts up up-real down smoke preload dev-ml dev-api dev-fe

PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
ML_BIN := ml/.venv/bin
ML_DESELECT := --deselect tests/test_address.py --deselect tests/test_config.py::test_paths_exist --deselect tests/test_db.py

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e '.[dev]'
	cd frontend && npm ci

# Тяжёлые зависимости ML (xgboost, lightgbm, catboost, polars) — в отдельном ml/.venv.
install-ml:
	$(PY) -m venv ml/.venv
	$(ML_BIN)/pip install -r ml/requirements.lock
	$(ML_BIN)/pip install --no-deps -e ml
	$(ML_BIN)/pip install pytest httpx

test: test-backend test-ml

test-backend:
	$(BIN)/pytest -q

# Три теста CAML требуют данных заказчика — как в CI, они исключены.
test-ml:
	cd ml && .venv/bin/python -m pytest -q $(ML_DESELECT)

lint:
	$(BIN)/ruff check backend tests scripts

contracts:
	$(BIN)/python scripts/export_contracts.py
	cd frontend && npm run gen:api

up:
	docker compose up -d --build

up-real:
	docker compose -f compose.yaml -f compose.real.yaml up -d --build

down:
	docker compose down

smoke:
	$(PY) scripts/smoke_compose.py

preload:
	$(PY) scripts/preload_demo.py

# Без Docker, из корня: backend читает .env текущего каталога. В .env для этого —
# DATABASE_URL=sqlite:///./data/dev.db и ML_URL=http://localhost:8001 (см. README).
dev-ml:
	PYTHONPATH=ml/src ML_MODE=stub $(BIN)/uvicorn mkl.product_api:app --port 8001

dev-api:
	$(BIN)/alembic -c backend/alembic.ini upgrade head
	PYTHONPATH=backend $(BIN)/python -m app.seed
	$(BIN)/uvicorn app.main:app --app-dir backend --reload --port 8000

dev-fe:
	cd frontend && npm run dev
