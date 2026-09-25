from typing import Literal

from pydantic import BaseModel, Field

from .common import Action, ReasonCode


class ReasonCodeOut(BaseModel):
    code: ReasonCode
    title: str
    actions: list[Action]


class TreeNode(BaseModel):
    id: str
    level: int
    kind: str
    name: str
    channels: int = 0
    picket_min: float | None = None
    picket_max: float | None = None
    children: list["TreeNode"] = Field(default_factory=list)


class GeoFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict
    properties: dict


class FeatureCollection(BaseModel):
    """Условная схема (ТЗ §7 GeoJSON). Координаты не географические — см. properties.note."""
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoFeature]
    properties: dict


class SyncReport(BaseModel):
    objects: int
    channels: int
    added: int
    changed: int
    removed: int
