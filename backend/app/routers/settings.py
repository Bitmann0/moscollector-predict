from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.misc import SettingsIn, SettingsOut
from ..security import CurrentUser, require_perm
from ..services import settings_store

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsOut, dependencies=[Depends(require_perm("admin"))])
def get_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return settings_store.get(db)


@router.put("/settings", response_model=SettingsOut)
def put_settings(body: SettingsIn, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_perm("admin"))) -> SettingsOut:
    return settings_store.put(db, body, user)
