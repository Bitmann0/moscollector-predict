"""Справочники: причины решений, дерево объектов, синхронизация.

reason_codes — живое: таблица reason_codes, её наполняет seed из vocabularies.json.

ЗАГЛУШКА — владелец BE-03 (tree) и BE-14 (sync) (C2).
Заменить: tree — сейчас дерево собирается из ref_objects/ref_channels, куда seed кладёт
синтетический справочник; BE-03 грузит туда реальный справочник, и функция остаётся
как есть, если не нужны новые поля. sync — сейчас отчёт с текущими числами объектов и
каналов и нулями изменений, нужна сверка с источником справочника.
Контракт: reason_codes, tree и sync не меняются; тест tests/test_endpoints_shape.py
должен остаться зелёным.
"""
import csv
import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..config import get_settings
from ..schemas.reference import ReasonCodeOut, SyncReport, TreeNode
from ..security import CurrentUser


def reason_codes(db: Session) -> list[ReasonCodeOut]:
    order = {code: i for i, code in enumerate(vocab.codes("reason_code"))}
    rows = sorted(db.scalars(select(models.ReasonCode)),
                  key=lambda r: (order.get(r.code, len(order)), r.code))
    return [ReasonCodeOut(code=r.code, title=r.title, actions=list(r.actions or []))
            for r in rows]


def channel_stats(db: Session) -> dict[str, tuple[int, float | None, float | None]]:
    """obj_id → (число каналов, минимальный пикет, максимальный пикет)."""
    rows = db.execute(select(models.RefChannel.obj_id, func.count(),
                             func.min(models.RefChannel.picket),
                             func.max(models.RefChannel.picket))
                      .group_by(models.RefChannel.obj_id))
    return {obj_id: (n, lo, hi) for obj_id, n, lo, hi in rows}


def _merge(values: list[float | None], pick) -> float | None:
    present = [v for v in values if v is not None]
    return pick(present) if present else None


def tree(db: Session) -> list[TreeNode]:
    objects = list(db.scalars(select(models.RefObject).order_by(models.RefObject.id)))
    ids = {o.id for o in objects}
    children: dict[str | None, list[models.RefObject]] = {}
    for o in objects:
        parent = o.parent_id if o.parent_id in ids else None
        children.setdefault(parent, []).append(o)
    stats = channel_stats(db)

    def build(obj: models.RefObject) -> TreeNode:
        kids = [build(c) for c in children.get(obj.id, [])]
        own, lo, hi = stats.get(obj.id, (0, None, None))
        return TreeNode(id=obj.id, level=obj.level, kind=obj.kind, name=obj.name,
                        channels=own + sum(k.channels for k in kids),
                        picket_min=_merge([lo, *(k.picket_min for k in kids)], min),
                        picket_max=_merge([hi, *(k.picket_max for k in kids)], max),
                        children=kids)

    return [build(o) for o in children.get(None, [])]


def sync(db: Session, user: CurrentUser) -> SyncReport:
    root = get_settings().raw_data_dir
    object_file = root / "справочник_объектов_диспетчер.csv"
    channel_file = root / "справочник_каналов_датчиков.csv"
    if not object_file.is_file() or not channel_file.is_file():
        objects = db.scalar(select(func.count()).select_from(models.RefObject)) or 0
        channels = db.scalar(select(func.count()).select_from(models.RefChannel)) or 0
        return SyncReport(objects=objects, channels=channels, added=0, changed=0, removed=0)
    with object_file.open(encoding="utf-8-sig", newline="") as stream:
        objects = list(csv.DictReader(stream))
    with channel_file.open(encoding="utf-8-sig", newline="") as stream:
        channels = list(csv.DictReader(stream))
    before = (db.scalar(select(func.count()).select_from(models.RefObject)) or 0,
              db.scalar(select(func.count()).select_from(models.RefChannel)) or 0)
    db.execute(delete(models.RefChannel))
    db.execute(delete(models.RefObject))
    db.add_all(models.RefObject(id=row["ид_объект"].strip(),
                                level=int(row["иерархия_уровень"]),
                                parent_id=(row.get("родитель") or "").strip() or None,
                                kind=(row.get("вид_объекта") or "unknown").strip(),
                                name=(row.get("диспетчерское_название_объекта") or "").strip())
               for row in objects)
    def picket(row: dict) -> float | None:
        match = re.search(r"ПК\s*(\d+(?:[.,]\d+)?)", row.get("название_датчика") or "", re.IGNORECASE)
        return float(match.group(1).replace(",", ".")) if match else None
    db.add_all(models.RefChannel(id=int(row["ид_канала_данных"]),
                                 obj_id=row["ид_объект"].strip(),
                                 system=(row.get("тип_инж_системы") or "").strip() or None,
                                 sensor_type=(row.get("тип_датчика") or "").strip() or None,
                                 tag=(row.get("тег_инженерной_системы") or "").strip() or None,
                                 name=(row.get("название_датчика") or "").strip() or None,
                                 picket=picket(row)) for row in channels)
    db.commit()
    added = max(len(objects) + len(channels) - sum(before), 0)
    return SyncReport(objects=len(objects), channels=len(channels), added=added,
                      changed=min(sum(before), len(objects) + len(channels)), removed=0)
