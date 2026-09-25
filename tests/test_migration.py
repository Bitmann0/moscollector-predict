"""Миграция 0001_initial совпадает со схемой моделей: таблицы, колонки, типы, индексы.

Идёт на временном SQLite; если задан TEST_DATABASE_URL — ещё и на нём (PostgreSQL в CI).
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from app import models  # noqa: F401  регистрирует таблицы в Base.metadata
from app.config import get_settings
from app.db import Base
from sqlalchemy import create_engine, inspect, pool, text

BACKEND = Path(__file__).resolve().parents[1] / "backend"


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL не задан"))]


@pytest.fixture(params=BACKENDS)
def migration_url(request, tmp_path, monkeypatch) -> str:
    if request.param == "sqlite":
        url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    else:
        url = os.environ["TEST_DATABASE_URL"]
        engine = create_engine(url, poolclass=pool.NullPool)
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


def _config() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    return cfg


def test_upgrade_head_matches_models(migration_url):
    command.upgrade(_config(), "head")
    engine = create_engine(migration_url, poolclass=pool.NullPool)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names()) - {"alembic_version"}
        assert tables == set(Base.metadata.tables)
        for name, table in Base.metadata.tables.items():
            columns = {c["name"] for c in insp.get_columns(name)}
            assert columns == set(table.columns.keys()), name
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


def test_downgrade_base_drops_everything(migration_url):
    cfg = _config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(migration_url, poolclass=pool.NullPool)
    try:
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")  # повторный подъём после отката проходит
