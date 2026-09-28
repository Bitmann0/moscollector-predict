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



OBJECTS_CSV = ('"ид_объект","иерархия_уровень","родитель","вид_объекта",'
               '"диспетчерское_название_объекта"\n'
               "5,1,,district,Район\n20,2,5,complex,объект Альфа\n"
               "5122,3,20,controlHouse,ДУ объект Альфа\n")
CHANNELS_CSV = ('"ид_канала_данных","тип_инж_системы","тип_датчика",'
                '"тег_инженерной_системы","название_датчика","ид_объект"\n'
                '120578,Охранная подсистема,КД АВ,"15-11.1.131.2.",КД АВ ПК28,5122\n')


def test_seed_syncs_real_reference_instead_of_synthetic(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw_real"
    raw.mkdir()
    (raw / "справочник_объектов_диспетчер.csv").write_text(OBJECTS_CSV, encoding="utf-8")
    (raw / "справочник_каналов_датчиков.csv").write_text(CHANNELS_CSV, encoding="utf-8")
    seed(db)  # сначала синтетика, как у стенда до появления бандла
    assert _count(db, models.RefObject) == 9
    monkeypatch.setenv("RAW_DATA_DIR", str(raw))
    get_settings.cache_clear()
    report = seed(db)
    assert report["reference_synced"]["objects"] == 3
    assert report["reference_loaded"] is False
    assert {o.name for o in db.scalars(select(models.RefObject))} == {
        "Район", "объект Альфа", "ДУ объект Альфа"}
    channel = db.get(models.RefChannel, 120578)
    assert (channel.obj_id, channel.picket) == ("5122", 28.0)
