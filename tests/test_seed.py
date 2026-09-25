"""seed: идемпотентность, обязательный DEMO_PASSWORD, справочник не перезаписывается."""
import pytest
from app import models, vocab
from app.config import get_settings
from app.security import verify_password
from app.seed import DEMO_ROLES, seed
from conftest import DEMO_PASSWORD
from sqlalchemy import func, select


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_seed_is_idempotent(db):
    first = seed(db)
    second = seed(db)
    assert first["reference_loaded"] is True and second["reference_loaded"] is False
    assert second["settings_added"] == 0
    assert _count(db, models.User) == len(DEMO_ROLES)
    assert _count(db, models.ReasonCode) == len(vocab.codes("reason_code"))
    assert _count(db, models.RefObject) == 9
    assert _count(db, models.RefChannel) == 30
    assert _count(db, models.Setting) == 3
    admin = db.get(models.User, "admin")
    assert admin.name == vocab.title("roles", "admin")
    assert verify_password(DEMO_PASSWORD, admin.password_hash)


def test_seed_needs_demo_password(db, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="DEMO_PASSWORD"):
        seed(db)


def test_seed_demo_off_keeps_only_dictionaries(db, monkeypatch):
    monkeypatch.setenv("SEED_DEMO", "0")
    monkeypatch.setenv("DEMO_PASSWORD", "")
    get_settings.cache_clear()
    seed(db)
    assert _count(db, models.User) == 0
    assert _count(db, models.RefObject) == 0
    assert _count(db, models.ReasonCode) == len(vocab.codes("reason_code"))


def test_seed_keeps_existing_reference_and_settings(db):
    db.add(models.RefObject(id="1", level=1, parent_id=None, kind="district", name="Район"))
    db.add(models.Setting(key="replay_speed", value={"value": 600}))
    db.commit()
    seed(db)
    assert _count(db, models.RefObject) == 1
    assert _count(db, models.RefChannel) == 0
    assert db.get(models.Setting, "replay_speed").value == {"value": 600}
