import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ImportRowStatus, ImportStatus, TransactionKind


class ImportRowOut(BaseModel):
    id: uuid.UUID
    row_number: int
    raw: list[Any]
    parsed: dict[str, Any]
    issues: list[dict[str, Any]]
    confidence: float
    status: ImportRowStatus
    transaction_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class ImportOut(BaseModel):
    id: uuid.UUID
    filename: str
    status: ImportStatus
    sheet_name: str | None
    header_row: int | None
    headers: list[Any] | None
    column_mapping: dict[str, int] | None
    stats: dict[str, Any] | None
    error: str | None
    created_at: datetime
    committed_at: datetime | None

    model_config = {"from_attributes": True}


class ImportDetailOut(ImportOut):
    rows: list[ImportRowOut]


class RemapIn(BaseModel):
    column_mapping: dict[str, int] = Field(
        description="Canonical field -> 0-based column index, e.g. {'date': 0, 'amount': 3}"
    )
    header_row: int | None = Field(default=None, ge=0)
    default_kind: TransactionKind | None = None


class RowUpdateIn(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    skip: bool | None = None
    product_is_new: bool | None = Field(
        default=None, description="Confirm an ambiguous product name is a new, different product"
    )
    allow_duplicate: bool | None = Field(
        default=None, description="Import this row even though it looks like a duplicate"
    )


class CommitIn(BaseModel):
    keep_unreviewed_as_provisional: bool = True
