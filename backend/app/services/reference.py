"""Причины решений, дерево объектов и синхронизация CSV-справочников."""
import csv
import re

from fastapi import HTTPException
from sqlalchemy import func, select
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


OBJECT_FILE = "справочник_объектов_диспетчер.csv"
CHANNEL_FILE = "справочник_каналов_датчиков.csv"
SOURCE_FILES = (OBJECT_FILE, CHANNEL_FILE)


def _validate_snapshot(objects: list[dict], channels: list[dict]) -> None:
    """Не допускаем частичный снимок, дубли и цикл до первого изменения БД.

    Справочник заказчика содержит объект с отсутствующим родителем; такой объект
    остаётся корнем в tree(), поэтому отсутствующего родителя не запрещаем.
    """
    if not objects or not channels:
        raise HTTPException(status_code=422, detail="reference_snapshot_empty")
    parents: dict[str, str | None] = {}
    try:
        for row in objects:
            object_id = row["ид_объект"].strip()
            parent = (row.get("родитель") or "").strip() or None
            name = (row.get("диспетчерское_название_объекта") or "").strip()
            if not object_id or len(object_id) > 32 or not name or len(name) > 300:
                raise ValueError("invalid_object")
            if object_id in parents:
                raise ValueError("duplicate_object")
            int(row["иерархия_уровень"])
            parents[object_id] = parent
        seen: set[int] = set()
        for row in channels:
            channel_id = int(row["ид_канала_данных"])
            obj_id = row["ид_объект"].strip()
            if channel_id in seen or obj_id not in parents:
                raise ValueError("duplicate_or_orphan_channel")
            seen.add(channel_id)
        for object_id in parents:
            path: set[str] = set()
            cursor = object_id
            while cursor in parents:
                if cursor in path:
                    raise ValueError("reference_cycle")
                path.add(cursor)
                cursor = parents[cursor]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid_reference_snapshot: {exc}") from exc


def sync(db: Session, user: CurrentUser) -> SyncReport:
    root = get_settings().raw_data_dir
    object_file = root / OBJECT_FILE
    channel_file = root / CHANNEL_FILE
    present = (object_file.is_file(), channel_file.is_file())
    if present.count(True) == 1:
        raise HTTPException(status_code=422, detail="reference_snapshot_incomplete")
    if not any(present):
        objects = db.scalar(select(func.count()).select_from(models.RefObject)) or 0
        channels = db.scalar(select(func.count()).select_from(models.RefChannel)) or 0
        return SyncReport(objects=objects, channels=channels, added=0, changed=0, removed=0)
    with object_file.open(encoding="utf-8-sig", newline="") as stream:
        objects = list(csv.DictReader(stream))
    with channel_file.open(encoding="utf-8-sig", newline="") as stream:
        channels = list(csv.DictReader(stream))
    _validate_snapshot(objects, channels)
    current_objects = {row.id: row for row in db.scalars(select(models.RefObject))}
    current_channels = {row.id: row for row in db.scalars(select(models.RefChannel))}
    incoming_object_ids: set[str] = set()
    incoming_channel_ids: set[int] = set()
    added = changed = removed = 0
    for item in objects:
        fields = {"level": int(item["иерархия_уровень"]),
                  "parent_id": (item.get("родитель") or "").strip() or None,
                  "kind": (item.get("вид_объекта") or "unknown").strip(),
                  "name": (item.get("диспетчерское_название_объекта") or "").strip()}
        object_id = item["ид_объект"].strip()
        incoming_object_ids.add(object_id)
        row = current_objects.get(object_id)
        if row is None:
            db.add(models.RefObject(id=object_id, **fields))
            added += 1
        elif any(getattr(row, key) != value for key, value in fields.items()):
            for key, value in fields.items():
                setattr(row, key, value)
            changed += 1
    def picket(row: dict) -> float | None:
        match = re.search(r"ПК\s*(\d+(?:[.,]\d+)?)", row.get("название_датчика") or "", re.IGNORECASE)
        return float(match.group(1).replace(",", ".")) if match else None
    for item in channels:
        channel_id = int(item["ид_канала_данных"])
        incoming_channel_ids.add(channel_id)
        fields = {"obj_id": item["ид_объект"].strip(),
                  "system": (item.get("тип_инж_системы") or "").strip() or None,
                  "sensor_type": (item.get("тип_датчика") or "").strip() or None,
                  "tag": (item.get("тег_инженерной_системы") or "").strip() or None,
                  "name": (item.get("название_датчика") or "").strip() or None,
                  "picket": picket(item)}
        row = current_channels.get(channel_id)
        if row is None:
            db.add(models.RefChannel(id=channel_id, **fields))
            added += 1
        elif any(getattr(row, key) != value for key, value in fields.items()):
            for key, value in fields.items():
                setattr(row, key, value)
            changed += 1
    for channel_id in current_channels.keys() - incoming_channel_ids:
        db.delete(current_channels[channel_id])
        removed += 1
    for object_id in current_objects.keys() - incoming_object_ids:
        db.delete(current_objects[object_id])
        removed += 1
    db.commit()
    return SyncReport(objects=len(objects), channels=len(channels), added=added,
                      changed=changed, removed=removed)
