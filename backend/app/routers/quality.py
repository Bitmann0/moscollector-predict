from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import Scenario
from ..schemas.misc import QualityOut
from ..security import require_perm
from ..services import quality

router = APIRouter(tags=["quality"])


@router.get("/quality", response_model=QualityOut, dependencies=[Depends(require_perm("view"))])
def weekly(scenario: Scenario, db: Session = Depends(get_db)) -> QualityOut:
    return quality.weekly(db, scenario)
