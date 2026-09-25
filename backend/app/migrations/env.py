"""Окружение Alembic. Адрес БД — DATABASE_URL через app.config, как у приложения.

Схема сравнивается с app.db.Base.metadata (все модели из app.models), поэтому
`alembic revision --autogenerate` видит новые колонки. Для SQLite включён batch-режим:
иначе ALTER TABLE в следующих миграциях там не работает.
"""
from logging.config import fileConfig
from pathlib import Path

import app.models  # noqa: F401  регистрирует таблицы в Base.metadata
from alembic import context
from app.config import get_settings
from app.db import Base
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=url.startswith("sqlite"),
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
