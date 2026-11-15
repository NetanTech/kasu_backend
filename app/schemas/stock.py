import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class CountItemIn(BaseModel):
    counted_qty: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    product_id: uuid.UUID | None = None
    product_name: str | None = Field(default=None, max_length=200)
    product_is_new: bool = False
    note: str | None = Field(default=None, max_length=500)


class StockCountIn(BaseModel):
    counted_on: date = Field(description="Count is applied at the end of this day")
    items: list[CountItemIn] = Field(min_length=1, max_length=500)


class StockCountOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    product_name: str | None
    unit: str | None
    counted_on: date
    counted_qty: Decimal
    expected_qty: Decimal | None = Field(
        description="What the records said was on hand just before this count"
    )
    variance_qty: Decimal | None = Field(description="counted - expected; negative = missing")
    variance_value: Decimal | None = Field(
        description="Variance valued at average cost; null if the cost is unknown"
    )
    expected_qty_at_entry: Decimal | None
    note: str | None
    created_at: datetime
    voided_at: datetime | None
    void_reason: str | None


class VoidCountIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
