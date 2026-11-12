import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import BusinessContext, get_business, require
from app.core.config import get_settings
from app.core.db import get_db
from app.models import ImportBatch, ImportRow
from app.models.enums import Permission, TransactionKind
from app.schemas.imports import (
    CommitIn,
    ImportDetailOut,
    ImportOut,
    ImportRowOut,
    RemapIn,
    RowUpdateIn,
)
from app.services.imports import service
from app.services.imports.detect import FIELDS
from app.services.imports.reader import UnsupportedFile

router = APIRouter(prefix="/businesses/{business_id}/imports", tags=["imports"])


def _get(db: Session, ctx: BusinessContext, import_id: uuid.UUID) -> ImportBatch:
    batch = db.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows))
        .where(ImportBatch.id == import_id, ImportBatch.business_id == ctx.id)
    )
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found")
    return batch


@router.post("", response_model=ImportDetailOut, status_code=status.HTTP_201_CREATED)
async def upload(
    file: UploadFile = File(...),
    sheet_name: str | None = Form(default=None),
    default_kind: TransactionKind | None = Form(
        default=None,
        description="What the rows are when the file doesn't say, e.g. 'sale' for a sales log",
    ),
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> ImportBatch:
    """Upload an Excel/CSV file. Rows are staged for review; nothing is in the ledger yet."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is too large")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty")
    try:
        batch = service.create_import(
            db,
            ctx.business,
            ctx.user.id,
            file.filename or "upload",
            file.content_type,
            data,
            sheet_name,
            default_kind,
        )
        db.commit()
    except UnsupportedFile as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return _get(db, ctx, batch.id)


@router.get("", response_model=list[ImportOut])
def list_imports(
    ctx: BusinessContext = Depends(get_business), db: Session = Depends(get_db)
) -> list[ImportBatch]:
    return list(
        db.scalars(
            select(ImportBatch)
            .where(ImportBatch.business_id == ctx.id)
            .order_by(ImportBatch.created_at.desc())
        )
    )


@router.get("/fields", response_model=list[str])
def mappable_fields() -> tuple[str, ...]:
    """Canonical fields a column can be mapped to."""
    return FIELDS


@router.get("/{import_id}", response_model=ImportDetailOut)
def get_import(
    import_id: uuid.UUID,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> ImportBatch:
    return _get(db, ctx, import_id)


@router.put("/{import_id}/mapping", response_model=ImportDetailOut)
def remap(
    import_id: uuid.UUID,
    body: RemapIn,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> ImportBatch:
    bad = set(body.column_mapping) - set(FIELDS)
    if bad:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown fields: {', '.join(sorted(bad))}"
        )
    batch = _get(db, ctx, import_id)
    try:
        service.remap_import(db, batch, body.column_mapping, body.header_row, body.default_kind)
        db.commit()
    except (service.ImportError_, UnsupportedFile) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.expire_all()
    return _get(db, ctx, import_id)


@router.patch("/{import_id}/rows/{row_id}", response_model=ImportRowOut)
def update_row(
    import_id: uuid.UUID,
    row_id: uuid.UUID,
    body: RowUpdateIn,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> ImportRow:
    """Answer KASU's questions about a row: fix values, skip it, or accept a flagged guess."""
    batch = _get(db, ctx, import_id)
    row = next((r for r in batch.rows if r.id == row_id), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Row not found")
    try:
        service.update_row(
            db, batch, row, body.changes, body.skip, body.product_is_new, body.allow_duplicate
        )
        db.commit()
    except service.ImportError_ as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return row


@router.post("/{import_id}/commit", response_model=ImportDetailOut)
def commit(
    import_id: uuid.UUID,
    body: CommitIn | None = None,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> ImportBatch:
    batch = _get(db, ctx, import_id)
    body = body or CommitIn()
    try:
        service.commit_import(
            db, ctx.business, batch, ctx.user.id, body.keep_unreviewed_as_provisional
        )
        db.commit()
    except service.ImportError_ as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return _get(db, ctx, import_id)
