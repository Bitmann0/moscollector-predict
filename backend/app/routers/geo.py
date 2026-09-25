from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.reference import FeatureCollection
from ..security import require_perm
from ..services import geo

router = APIRouter(tags=["schema"])


@router.get("/schema.geojson", response_model=FeatureCollection,
            dependencies=[Depends(require_perm("view"))])
def schema_geojson(complex: str | None = None,
                   db: Session = Depends(get_db)) -> FeatureCollection:
    return geo.schema_geojson(db, complex)


@router.get("/schema.wkt", response_class=PlainTextResponse,
            dependencies=[Depends(require_perm("view"))])
def schema_wkt(complex: str | None = None, db: Session = Depends(get_db)) -> str:
    return geo.schema_wkt(db, complex)
