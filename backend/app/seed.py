"""Начальное наполнение БД: `python -m app.seed`. Живое, идемпотентно.

Порядок старта контейнера: alembic upgrade head → seed → uvicorn.

Всегда:
- reason_codes из contracts/vocabularies.json (обновляются по коду);
- строки settings по умолчанию, если их нет (изменённые админом не трогаются).

При SEED_DEMO=1 дополнительно:
- по пользователю на роль dispatcher, technician, analyst, manager, admin; логин равен
  коду роли, имя — название роли из словаря, пароль — DEMO_PASSWORD (обязателен);
- синтетический справочник contracts/synthetic_reference.json, только если ref_objects и
  ref_channels пусты: реальный справочник BE-03 не перезаписывается.

Прогнозов seed не создаёт: их создаёт run-daily, так проверяется сквозная труба.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models, vocab
from .config import get_settings
from .db import session_factory
from .security import hash_password, verify_password
from .services import settings_store
from .services.helpers import synthetic_reference

DEMO_ROLES = ["dispatcher", "technician", "analyst", "manager", "admin"]


def seed_reason_codes(db: Session) -> int:
    for item in vocab.load()["reason_code"]:
        row = db.get(models.ReasonCode, item["code"])
        if row is None:
            db.add(models.ReasonCode(code=item["code"], title=item["title"],
                                     actions=list(item["actions"])))
        else:
            row.title = item["title"]
            row.actions = list(item["actions"])
    return len(vocab.load()["reason_code"])


def seed_settings(db: Session) -> int:
    added = 0
    for key, value in settings_store.defaults().items():
        if db.get(models.Setting, key) is None:
            db.add(models.Setting(key=key, value={"value": value}))
            added += 1
    return added


def seed_users(db: Session, password: str) -> int:
    if not password:
        raise RuntimeError("DEMO_PASSWORD не задан: демо-пользователи создаются с общим паролем "
                           "из .env (см. .env.example) или SEED_DEMO=0")
    for role in DEMO_ROLES:
        name = vocab.title("roles", role)
        row = db.get(models.User, role)
        if row is None:
            db.add(models.User(login=role, name=name, role=role,
                               password_hash=hash_password(password)))
            continue
        row.name = name
        row.role = role
        if not verify_password(password, row.password_hash):
            row.password_hash = hash_password(password)
    return len(DEMO_ROLES)


def seed_reference(db: Session) -> bool:
    has_objects = db.scalar(select(func.count()).select_from(models.RefObject))
    has_channels = db.scalar(select(func.count()).select_from(models.RefChannel))
    if has_objects or has_channels:
        return False
    ref = synthetic_reference()
    db.add_all(models.RefObject(id=o["id"], level=o["level"], parent_id=o["parent_id"],
                                kind=o["kind"], name=o["name"]) for o in ref["objects"])
    db.add_all(models.RefChannel(id=c["id"], obj_id=c["obj_id"], system=c.get("system"),
                                 sensor_type=c.get("sensor_type"), tag=c.get("tag"),
                                 name=c.get("name"), picket=c.get("picket"))
               for c in ref["channels"])
    return True


def seed(db: Session) -> dict:
    """Всё наполнение одной транзакцией. Возвращает, что сделано, — для лога и тестов."""
    settings = get_settings()
    report = {"reason_codes": seed_reason_codes(db), "settings_added": seed_settings(db),
              "users": 0, "reference_loaded": False}
    if settings.seed_demo:
        report["users"] = seed_users(db, settings.demo_password)
        report["reference_loaded"] = seed_reference(db)
    db.commit()
    return report


def main() -> int:
    with session_factory()() as db:
        report = seed(db)
    print(f"seed: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
