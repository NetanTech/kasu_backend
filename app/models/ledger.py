"""The ledger.

`transactions` holds the *current* state of each transaction (a projection).
`transaction_events` is append-only and records every create/correct/confirm/void with a full
snapshot, so history is never silently overwritten and the ledger can be rebuilt (PRD §20).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk, str_enum, utcnow
from app.models.enums import (
    Settlement,
    TransactionEventType,
    TransactionKind,
    TransactionSource,
    TransactionStatus,
)
from app.models.memory import Counterparty, Product


class Transaction(UUIDPk, CreatedAt, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Idempotency for offline/store-and-forward clients (PRD §7.4).
        UniqueConstraint("business_id", "client_ref"),
        Index("ix_transactions_business_date", "business_id", "occurred_on"),
        Index("ix_transactions_business_dedupe", "business_id", "dedupe_key"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[TransactionKind] = mapped_column(str_enum(TransactionKind))
    status: Mapped[TransactionStatus] = mapped_column(
        str_enum(TransactionStatus), default=TransactionStatus.CONFIRMED
    )
    settlement: Mapped[Settlement] = mapped_column(str_enum(Settlement), default=Settlement.PAID)
    occurred_on: Mapped[date] = mapped_column(Date)

    # Money is stored in minor units (kobo for NGN) to avoid float errors.
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")

    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit: Mapped[str | None] = mapped_column(String(50))
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparties.id", ondelete="SET NULL"), index=True
    )
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)

    source: Mapped[TransactionSource] = mapped_column(str_enum(TransactionSource))
    source_ref: Mapped[str | None] = mapped_column(String(200))  # e.g. "import_row:<uuid>"
    client_ref: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    dedupe_key: Mapped[str] = mapped_column(String(64))

    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    product: Mapped[Product | None] = relationship()
    counterparty: Mapped[Counterparty | None] = relationship()
    events: Mapped[list["TransactionEvent"]] = relationship(
        back_populates="transaction", order_by="TransactionEvent.created_at"
    )


class TransactionEvent(UUIDPk, CreatedAt, Base):
    __tablename__ = "transaction_events"

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[TransactionEventType] = mapped_column(str_enum(TransactionEventType))
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSONB)  # full state after this event
    changes: Mapped[dict | None] = mapped_column(JSONB)  # {field: [old, new]}
    reason: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    transaction: Mapped[Transaction] = relationship(back_populates="events")
