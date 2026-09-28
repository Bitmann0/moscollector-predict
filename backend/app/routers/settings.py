from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.misc import SettingsIn, SettingsOut
from ..schemas.parameters import ParametersIn, ParametersOut
from ..security import CurrentUser, require_perm
from ..services import parameters, settings_store

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsOut, dependencies=[Depends(require_perm("admin"))])
def get_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return settings_store.get(db)


@router.put("/settings", response_model=SettingsOut)
def put_settings(body: SettingsIn, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_perm("admin"))) -> SettingsOut:
    return settings_store.put(db, body, user)


@router.get("/settings/parameters", response_model=ParametersOut,
            dependencies=[Depends(require_perm("admin"))])
def get_parameters(db: Session = Depends(get_db)) -> ParametersOut:
    """Настраиваемые параметры (ТЗ §18): действующие, проверенные и диапазоны полей."""
    return parameters.out(parameters.load(db))


@router.put("/settings/parameters", response_model=ParametersOut,
            responses={403: {"description": "нет права admin или settings_locked на стенде"},
                       409: {"description": "parameters_version_conflict: параметры уже "
                                            "изменил другой администратор"}})
def put_parameters(body: ParametersIn, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_perm("admin"))) -> ParametersOut:
    """Новые события классифицируются по новым параметрам сразу, принятые раньше — после
    POST /admin/reclassify-events. Лимиты действуют со следующего дневного расчёта.
    Лимит выше проверенного — 422 limit_above_verified."""
    result, diff = parameters.put(db, body, user.login)
    request.state.audit_payload = {"version": result.version, "changed": diff}
    return result
