"""Подключение к БД. Живое.

PostgreSQL 16 в compose и CI (ТЗ §11: PostgreSQL 12+), SQLite — только в локальных
тестах. Поэтому модели используют переносимые типы (JSON, DateTime(timezone=True)).
"""
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            _engine = create_engine(url, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """Для тестов: следующий вызов get_engine() перечитает DATABASE_URL."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Iterator[Session]:
    with session_factory()() as session:
        yield session
