"""Таблицы БД (задача BE-02 плана). Живое: колонки добавляются новой миграцией.

Одна строка `forecasts` — один прогноз или недельная рекомендация: id совпадает с
alert_id или recommendation_id из ML, поэтому повторный расчёт того же дня
обновляет строку, а история изменений копится в `forecast_versions`.
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# BIGINT с автоинкрементом, который работает и в SQLite (там автоинкремент — только INTEGER).
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    __tablename__ = "users"
    login: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(300))


class RevokedSession(Base):
    """Сессии, завершённые выходом (jti из токена cookie, миграция 0003). Строка нужна,
    пока токен не истёк по возрасту: дальше его отклоняет подпись с max_age."""
    __tablename__ = "revoked_sessions"
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RefObject(Base):
    """Район → комплекс → объект (справочник_объектов_диспетчер.csv)."""
    __tablename__ = "ref_objects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    level: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(300))


class RefChannel(Base):
    """Канал данных (справочник_каналов_датчиков.csv)."""
    __tablename__ = "ref_channels"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    obj_id: Mapped[str] = mapped_column(String(32), index=True)
    system: Mapped[str | None] = mapped_column(String(100))
    sensor_type: Mapped[str | None] = mapped_column(String(100))
    tag: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(Text)
    picket: Mapped[float | None] = mapped_column(Float)


class ForecastRun(Base):
    __tablename__ = "forecast_runs"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asof: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # daily | weekly_guard
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heads: Mapped[dict] = mapped_column(JSON, default=dict)  # статус каждой головы из C1
    raw: Mapped[dict | None] = mapped_column(JSON)


class Forecast(Base):
    __tablename__ = "forecasts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    scenario: Mapped[str] = mapped_column(String(32), index=True)
    head: Mapped[str] = mapped_column(String(32))
    asof: Mapped[date] = mapped_column(Date, index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon_hours: Mapped[int] = mapped_column(Integer)
    score_type: Mapped[str] = mapped_column(String(32))
    risk: Mapped[float | None] = mapped_column(Float)
    priority_score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer)
    in_budget: Mapped[bool] = mapped_column(Boolean, default=True)
    obj_id: Mapped[str | None] = mapped_column(String(32), index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    address: Mapped[dict] = mapped_column(JSON, default=dict)
    factors: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # evidence, счётчики недельной очереди
    data_status: Mapped[str] = mapped_column(String(16), default="ok")
    case_key: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16))
    first_run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id"))
    last_run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id"))


class ForecastVersion(Base):
    __tablename__ = "forecast_versions"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id"))
    risk: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class IssuedLog(Base):
    """Журнал выданного для пауз ML (C1): строки /score с in_budget=true в лимите показа."""
    __tablename__ = "issued_log"
    __table_args__ = (UniqueConstraint("head", "asof", "entity_key"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    head: Mapped[str] = mapped_column(String(32), index=True)
    asof: Mapped[date] = mapped_column(Date, index=True)
    entity_key: Mapped[str] = mapped_column(String(64))  # "channel:104049" или "obj:3215"
    forecast_id: Mapped[str] = mapped_column(String(64))


class ReasonCode(Base):
    __tablename__ = "reason_codes"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    actions: Mapped[list] = mapped_column(JSON, default=list)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="live")


class Outcome(Base):
    __tablename__ = "outcomes"
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), primary_key=True)
    outcome_auto: Mapped[str | None] = mapped_column(String(16))
    outcome_manual: Mapped[str | None] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(Text)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    author: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="live")


class ManualOutcomeRevision(Base):
    """Неизменяемая история ручных итогов; текущий итог остаётся в outcomes."""
    __tablename__ = "manual_outcome_revisions"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), index=True)
    outcome: Mapped[str | None] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(Text)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    author: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16))


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    forecast_ids: Mapped[list] = mapped_column(JSON, default=list)
    scenario: Mapped[str] = mapped_column(String(32), index=True)
    obj_id: Mapped[str | None] = mapped_column(String(32), index=True)
    priority: Mapped[str] = mapped_column(String(16))
    work_type: Mapped[str] = mapped_column(String(200))
    due_by: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), index=True)
    rationale: Mapped[list] = mapped_column(JSON, default=list)
    pickets: Mapped[list] = mapped_column(JSON, default=list)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="live")


class WorkOrderHistory(Base):
    __tablename__ = "work_order_history"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    author: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IngestBatch(Base):
    __tablename__ = "ingest_batches"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # smvu | ods | reset
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    outside_demo_window: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))


class Event(Base):
    """Событие журнала СМВУ. row_hash — хеш нормализованного полного кортежа (C5).

    incident_group — группа аварии из vocabularies.json (миграция 0002). Индекс
    частичный: группа есть у 13 637 из 10 428 318 событий стенда. count(*) по группе
    с ним — 1,7 мс, а тот же запрос по event_class без индекса читает таблицу целиком
    за 377 мс (docs/submission/perf/reclassify_0928.txt).
    """
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_incident_group", "incident_group",
                            postgresql_where=text("incident_group IS NOT NULL"),
                            sqlite_where=text("incident_group IS NOT NULL")),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    alarm: Mapped[bool] = mapped_column(Boolean)
    val_raw: Mapped[str | None] = mapped_column(Text)
    val_num: Mapped[float | None] = mapped_column(Float)
    event_class: Mapped[str] = mapped_column(String(16))
    hint: Mapped[str | None] = mapped_column(String(200))
    incident_group: Mapped[str | None] = mapped_column(String(32))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_batches.id"))
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)


class OdsRecord(Base):
    __tablename__ = "ods_journal"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    obj_id: Mapped[str | None] = mapped_column(String(32))
    record_type: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_batches.id"))
    ingest_key: Mapped[str | None] = mapped_column(String(64), unique=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    read_by: Mapped[list] = mapped_column(JSON, default=list)


class AuditRecord(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    user_login: Mapped[str | None] = mapped_column(String(64), index=True)
    role: Mapped[str | None] = mapped_column(String(32))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(500))
    status: Mapped[int] = mapped_column(Integer)
    entity: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict | None] = mapped_column(JSON)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
