"""Recording and reviewing physical stock counts."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Product, StockCount
from app.models.base import utcnow
from app.services.analytics import LedgerView, StockAdjustment, money
from app.services.memory import resolve_product


class CountError(Exception):
    def __init__(self, message: str, suggestion: str | None = None):
        super().__init__(message)
        self.suggestion = suggestion


@dataclass
class CountItem:
    counted_qty: Decimal
    product_id: uuid.UUID | None = None
    product_name: str | None = None
    product_is_new: bool = False
    note: str | None = None


def record_counts(
    db: Session,
    business: Business,
    user_id: uuid.UUID,
    counted_on: date,
    items: list[CountItem],
) -> list[StockCount]:
    if counted_on > date.today() + timedelta(days=1):
        raise CountError("A stock count can't be dated in the future")

    product_ids: list[uuid.UUID] = []
    for item in items:
        if item.counted_qty < 0:
            raise CountError("Counted quantity can't be negative")
        if item.product_id:
            # Scoped to this business: a product id from another tenant is "unknown".
            if not db.scalar(
                select(Product.id).where(
                    Product.id == item.product_id, Product.business_id == business.id
                )
            ):
                raise CountError("Unknown product_id")
            pid = item.product_id
        elif item.product_name:
            res = resolve_product(db, business.id, item.product_name, force_new=item.product_is_new)
            if res.entity is None:
                raise CountError(res.issue or "Unknown product", res.suggestion)
            pid = res.entity.id
        else:
            raise CountError("Each item needs a product_id or product_name")
        if pid in product_ids:
            raise CountError("The same product appears twice in this count")
        product_ids.append(pid)

    # What the records said just before this count (end of the count day).
    view = LedgerView(db, business.id, counted_on)
    counts = []
    for pid, item in zip(product_ids, items, strict=True):
        state = view.stock.get(pid)
        count = StockCount(
            business_id=business.id,
            product_id=pid,
            counted_on=counted_on,
            counted_qty=item.counted_qty,
            note=item.note,
            expected_qty_at_entry=state.qty if state else Decimal(0),
            created_by=user_id,
        )
        db.add(count)
        counts.append(count)
    db.flush()
    return counts


def void_count(
    db: Session, count: StockCount, user_id: uuid.UUID, reason: str | None
) -> StockCount:
    if count.voided_at is not None:
        raise CountError("This count is already voided")
    count.voided_at = utcnow()
    count.voided_by = user_id
    count.void_reason = reason
    return count


def describe(counts: list[StockCount], view: LedgerView) -> list[dict]:
    """Attach the live variance (recomputed from the ledger as it stands now)."""
    by_id: dict[uuid.UUID, StockAdjustment] = {a.count.id: a for a in view.adjustments}
    out = []
    for c in counts:
        adj = by_id.get(c.id)
        product = view.products.get(c.product_id)
        out.append(
            {
                "id": c.id,
                "product_id": c.product_id,
                "product_name": product.name if product else None,
                "unit": product.unit if product else None,
                "counted_on": c.counted_on,
                "counted_qty": c.counted_qty,
                "expected_qty": adj.expected_qty if adj else None,
                "variance_qty": adj.variance_qty if adj else None,
                "variance_value": (
                    money(adj.value_minor) if adj and adj.value_minor is not None else None
                ),
                "expected_qty_at_entry": c.expected_qty_at_entry,
                "note": c.note,
                "created_at": c.created_at,
                "voided_at": c.voided_at,
                "void_reason": c.void_reason,
            }
        )
    return out
