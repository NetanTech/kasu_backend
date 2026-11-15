"""Physical stock counts: what the owner actually found on the shelf.

A count is applied at the *end* of `counted_on` (after that day's sales and purchases) and
resets the ledger's estimated quantity for the product. Counts are never edited; a wrong count
is voided with a reason and a new one recorded, so the history stays auditable (PRD §20).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk
from app.models.memory import Product


class StockCount(UUIDPk, CreatedAt, Base):
    __tablename__ = "stock_counts"
    __table_args__ = (
        Index("ix_stock_counts_business_product_date", "business_id", "product_id", "counted_on"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    counted_on: Mapped[date] = mapped_column(Date)
    counted_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    note: Mapped[str | None] = mapped_column(Text)

    # What the ledger expected at the time the count was recorded (kept for audit; the live
    # variance is recomputed from the ledger, since earlier records can be corrected later).
    expected_qty_at_entry: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    void_reason: Mapped[str | None] = mapped_column(String(500))

    product: Mapped[Product] = relationship()
