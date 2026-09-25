from datetime import date

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import Page
from ..schemas.events import EventRowIn, IngestBatchOut, OdsRowIn, ResetDayOut
from ..security import CurrentUser, require_perm
from ..services import ingest

router = APIRouter(tags=["ingest"])

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # лимит файла; указан в документации (ML2-07)


@router.post("/ingest/events", response_model=IngestBatchOut, status_code=201)
def ingest_rows(rows: list[EventRowIn], db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_perm("ingest"))) -> IngestBatchOut:
    """Пачка журнала СМВУ в JSON (до 5 000 строк) — так шлёт replay.py."""
    return ingest.ingest_rows(db, rows, user)


@router.post("/ingest/events/upload", response_model=IngestBatchOut, status_code=201)
async def ingest_file(file: UploadFile = File(...), db: Session = Depends(get_db),
                      user: CurrentUser = Depends(require_perm("ingest"))) -> IngestBatchOut:
    """Файл журнала СМВУ: CSV как журнал_событий_пример.csv или XLSX (ТЗ §7)."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    return ingest.ingest_file(db, file.filename or "upload", content, user)


@router.delete("/ingest/day/{day}", response_model=ResetDayOut)
def reset_day(day: date, db: Session = Depends(get_db),
              user: CurrentUser = Depends(require_perm("integration", "admin"))) -> ResetDayOut:
    return ingest.reset_day(db, day, user)


@router.post("/ingest/ods-journal", response_model=IngestBatchOut, status_code=201)
def ingest_ods(rows: list[OdsRowIn], db: Session = Depends(get_db),
               user: CurrentUser = Depends(require_perm("ingest"))) -> IngestBatchOut:
    return ingest.ingest_ods(db, rows, user)


@router.get("/ingest/batches", response_model=Page[IngestBatchOut],
            dependencies=[Depends(require_perm("ingest"))])
def list_batches(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                 db: Session = Depends(get_db)) -> Page[IngestBatchOut]:
    return ingest.list_batches(db, page, page_size)
