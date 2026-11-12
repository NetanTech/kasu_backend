import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import BusinessContext, get_business
from app.core.db import get_db
from app.models import StockCount
from app.schemas.stock import StockCountIn, StockCountOut, VoidCountIn
from app.services.analytics import LedgerView
from app.services.stock_counts import CountError, CountItem, describe, record_counts, void_count

router = APIRouter(prefix="/businesses/{business_id}/stock-counts", tags=["stock counts"])


def _view(db: Session, ctx: BusinessContext, counts: list[StockCount]) -> LedgerView:
    until = max([date.today(), *(c.counted_on for c in counts)])
    return LedgerView(db, ctx.id, until)


@router.post("", response_model=list[StockCountOut], status_code=status.HTTP_201_CREATED)
def create_counts(
    body: StockCountIn,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Record what's physically on the shelf. Returns the difference from your records."""
    items = [CountItem(**i.model_dump()) for i in body.items]
    try:
        counts = record_counts(db, ctx.business, ctx.user.id, body.counted_on, items)
        db.commit()
    except CountError as exc:
        db.rollback()
        detail = {"message": str(exc), "suggestion": exc.suggestion} if exc.suggestion else str(exc)
        code = status.HTTP_409_CONFLICT if exc.suggestion else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, detail) from None
    return describe(counts, _view(db, ctx, counts))


@router.get("", response_model=list[StockCountOut])
def list_counts(
    product_id: uuid.UUID | None = None,
    start: date | None = None,
    end: date | None = None,
    include_voided: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(StockCount).where(StockCount.business_id == ctx.id)
    if product_id:
        stmt = stmt.where(StockCount.product_id == product_id)
    if start:
        stmt = stmt.where(StockCount.counted_on >= start)
    if end:
        stmt = stmt.where(StockCount.counted_on <= end)
    if not include_voided:
        stmt = stmt.where(StockCount.voided_at.is_(None))
    counts = list(
        db.scalars(
            stmt.order_by(StockCount.counted_on.desc(), StockCount.created_at.desc()).limit(limit)
        )
    )
    return describe(counts, _view(db, ctx, counts))


@router.post("/{count_id}/void", response_model=StockCountOut)
def void(
    count_id: uuid.UUID,
    body: VoidCountIn,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> dict:
    """Retract a wrong count. It stays in history but no longer affects stock or profit."""
    count = db.scalar(
        select(StockCount).where(StockCount.id == count_id, StockCount.business_id == ctx.id)
    )
    if count is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock count not found")
    try:
        void_count(db, count, ctx.user.id, body.reason)
        db.commit()
    except CountError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return describe([count], _view(db, ctx, [count]))[0]
