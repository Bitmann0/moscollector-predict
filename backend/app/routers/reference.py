from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.reference import ReasonCodeOut, SyncReport, TreeNode
from ..security import CurrentUser, require_perm
from ..services import reference

router = APIRouter(tags=["reference"])


@router.get("/reason-codes", response_model=list[ReasonCodeOut],
            dependencies=[Depends(require_perm("view"))])
def reason_codes(db: Session = Depends(get_db)) -> list[ReasonCodeOut]:
    return reference.reason_codes(db)


@router.get("/reference/tree", response_model=list[TreeNode],
            dependencies=[Depends(require_perm("view"))])
def tree(db: Session = Depends(get_db)) -> list[TreeNode]:
    return reference.tree(db)


@router.post("/reference/sync", response_model=SyncReport)
def sync(db: Session = Depends(get_db),
         user: CurrentUser = Depends(require_perm("admin"))) -> SyncReport:
    return reference.sync(db, user)
