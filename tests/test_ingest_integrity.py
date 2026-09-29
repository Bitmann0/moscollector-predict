"""Согласованность при повторной загрузке и сбросе данных СМВУ/ОДС."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor

import openpyxl
import pytest
from app import models
from app.config import get_settings
from app.db import session_factory
from app.schemas.events import EventRowIn
from app.security import CurrentUser
from app.services import ingest
from sqlalchemy import func, select

API = "/api/v1"
EVENT = {"ид_события": 900, "ид_канала_данных": 9000004, "дата": "2026-06-30",
         "время": "10:00:00", "тревожное": "t", "значение_датчика": "Обнаружен газ"}


def test_reset_day_removes_alarm_notifications(admin, db):
    created = admin.post(f"{API}/ingest/events", json=[EVENT])
    assert created.status_code == 201
    assert db.scalar(select(func.count()).select_from(models.Notification)) == 1

    reset = admin.delete(f"{API}/ingest/day/2026-06-30")
    assert reset.status_code == 200
    assert reset.json()["deleted_events"] == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(models.Event)) == 0
    assert db.scalar(select(func.count()).select_from(models.Notification)
                     .where(models.Notification.kind == "event.alarm")) == 0


def test_ods_repeat_has_database_ingest_key(integration, db):
    row = {"ts": "2026-06-30T10:00:00+03:00", "obj_id": "9101",
           "record_type": "inspection", "decision": "checked", "reason": "проверено"}
    first = integration.post(f"{API}/ingest/ods-journal", json=[row])
    second = integration.post(f"{API}/ingest/ods-journal", json=[row])
    assert first.status_code == second.status_code == 201
    assert first.json()["accepted"] == second.json()["duplicates"] == 1
    stored = db.scalar(select(models.OdsRecord))
    assert stored is not None and len(stored.ingest_key) == 64


def test_bad_reference_snapshot_does_not_replace_existing(admin, db, tmp_path, monkeypatch):
    objects = tmp_path / "справочник_объектов_диспетчер.csv"
    objects.write_text(
        "ид_объект,иерархия_уровень,родитель,вид_объекта,диспетчерское_название_объекта\n"
        "1,1,,district,Район\n1,2,1,complex,Дубль\n", encoding="utf-8")
    channels = tmp_path / "справочник_каналов_датчиков.csv"
    channels.write_text("ид_канала_данных,ид_объект\n123,1\n", encoding="utf-8")
    before = db.scalar(select(func.count()).select_from(models.RefObject))
    monkeypatch.setenv("RAW_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        response = admin.post(f"{API}/reference/sync")
        assert response.status_code == 422
        db.expire_all()
        assert db.scalar(select(func.count()).select_from(models.RefObject)) == before
    finally:
        get_settings.cache_clear()


def test_incomplete_reference_snapshot_is_rejected(admin, tmp_path, monkeypatch):
    (tmp_path / "справочник_объектов_диспетчер.csv").write_text(
        "ид_объект,иерархия_уровень,родитель,вид_объекта,диспетчерское_название_объекта\n"
        "1,1,,district,Район\n", encoding="utf-8")
    monkeypatch.setenv("RAW_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        response = admin.post(f"{API}/reference/sync")
        assert response.status_code == 422
        assert response.json()["detail"] == "reference_snapshot_incomplete"
    finally:
        get_settings.cache_clear()


def test_file_row_limit_rejects_before_writing(admin, db, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_UPLOAD_ROWS", 1)
    header = ",".join(EVENT)
    values = ",".join(str(value) for value in EVENT.values())
    content = f"{header}\n{values}\n{values}\n".encode()
    response = admin.post(f"{API}/ingest/events/upload",
                          files={"file": ("events.csv", content, "text/csv")})
    assert response.status_code == 413
    assert response.json()["detail"] == "file_too_many_rows"
    assert db.scalar(select(func.count()).select_from(models.Event)) == 0


def test_empty_upload_is_rejected(admin):
    response = admin.post(f"{API}/ingest/events/upload",
                          files={"file": ("empty.csv", b"", "text/csv")})
    assert response.status_code == 201
    assert response.json()["status"] == "rejected"


def test_xlsx_decompressed_limit_rejects_before_parsing(admin, monkeypatch):
    book = openpyxl.Workbook()
    book.active.append(list(EVENT))
    book.active.append(list(EVENT.values()))
    output = io.BytesIO()
    book.save(output)
    monkeypatch.setattr(ingest, "MAX_XLSX_UNCOMPRESSED_BYTES", 1)
    response = admin.post(f"{API}/ingest/events/upload",
                          files={"file": ("events.xlsx", output.getvalue(),
                                          "application/octet-stream")})
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_uncompressed_too_large"


def test_parallel_event_retry_on_postgres(seeded, monkeypatch):
    if seeded.get_bind().dialect.name != "postgresql":
        pytest.skip("нужен PostgreSQL для проверки конкурентной загрузки")
    barrier = threading.Barrier(2, timeout=5)
    guard = threading.Lock()
    calls = 0
    original = ingest._existing_hashes

    def synchronized(db, hashes):
        nonlocal calls
        seen = original(db, hashes)
        with guard:
            calls += 1
            first_attempt = calls <= 2
        if first_attempt:
            barrier.wait()
        return seen

    monkeypatch.setattr(ingest, "_existing_hashes", synchronized)
    row = EventRowIn.model_validate(EVENT)
    user = CurrentUser("integration", "Интеграция", "integration", frozenset())
    def upload():
        with session_factory()() as db:
            return ingest.ingest_rows(db, [row], user, notify=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(upload) for _ in range(2)]
        results = [job.result(timeout=10) for job in jobs]
    assert sorted((result.accepted, result.duplicates) for result in results) == [(0, 1), (1, 0)]
    seeded.expire_all()
    assert seeded.scalar(select(func.count()).select_from(models.Event)) == 1
