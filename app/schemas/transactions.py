import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.models.enums import (
    Settlement,
    TransactionEventType,
    TransactionKind,
    TransactionSource,
    TransactionStatus,
)
from app.schemas.common import ORM, Money
from app.services.normalization import from_minor


class TransactionCreate(BaseModel):
    kind: TransactionKind
    occurred_on: date
    amount: Money
    settlement: Settlement = Settlement.PAID
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    unit: str | None = Field(default=None, max_length=50)
    product_id: uuid.UUID | None = None
    product_name: str | None = Field(default=None, max_length=200)
    product_is_new: bool = False
    counterparty_name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    client_ref: str | None = Field(
        default=None,
        max_length=200,
        description="Client-generated id. Re-sending the same client_ref returns the original "
        "transaction instead of creating a duplicate (offline sync).",
    )


class TransactionCorrect(BaseModel):
    kind: TransactionKind | None = None
    occurred_on: date | None = None
    amount: Money | None = None
    settlement: Settlement | None = None
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    unit: str | None = Field(default=None, max_length=50)
    product_id: uuid.UUID | None = None
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=500)


class VoidIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TransactionOut(ORM):
    id: uuid.UUID
    kind: TransactionKind
    status: TransactionStatus
    settlement: Settlement
    occurred_on: date
    amount_minor: int
    currency: str
    quantity: Decimal | None
    unit: str | None
    product_id: uuid.UUID | None
    product_name: str | None = None
    counterparty_id: uuid.UUID | None
    counterparty_name: str | None = None
    category: str | None
    description: str | None
    source: TransactionSource
    source_ref: str | None
    client_ref: str | None
    confidence: float
    version: int
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def amount(self) -> Decimal:
        return from_minor(self.amount_minor)


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


class TransactionEventOut(ORM):
    id: uuid.UUID
    event_type: TransactionEventType
    version: int
    snapshot: dict[str, Any]
    changes: dict[str, Any] | None
    reason: str | None
    actor_user_id: uuid.UUID | None
    created_at: datetime
