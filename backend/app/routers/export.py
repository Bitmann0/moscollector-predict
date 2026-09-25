from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import require_perm
from ..services import export

router = APIRouter(tags=["export"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/export/forecasts.xlsx", response_class=Response,
            responses={200: {"content": {XLSX: {}}}},
            dependencies=[Depends(require_perm("export"))])
def forecasts_xlsx(date_from: date | None = Query(None, alias="from"),
                   date_to: date | None = Query(None, alias="to"),
                   db: Session = Depends(get_db)) -> Response:
    body = export.forecasts_xlsx(db, date_from, date_to)
    return Response(body, media_type=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="forecasts.xlsx"'})
