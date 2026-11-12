"""Staged imports: a file is parsed into rows the user reviews before anything hits the ledger."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk, str_enum
from app.models.enums import ImportRowStatus, ImportStatus


class ImportBatch(UUIDPk, CreatedAt, Base):
    __tablename__ = "import_batches"

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    storage_path: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    sheet_name: Mapped[str | None] = mapped_column(String(100))
    header_row: Mapped[int | None] = mapped_column(Integer)  # 0-based index in the sheet
    headers: Mapped[list | None] = mapped_column(JSONB)
    column_mapping: Mapped[dict | None] = mapped_column(JSONB)  # {field: column_index}
    status: Mapped[ImportStatus] = mapped_column(
        str_enum(ImportStatus), default=ImportStatus.NEEDS_REVIEW
    )
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rows: Mapped[list["ImportRow"]] = relationship(
        back_populates="batch", order_by="ImportRow.row_number", cascade="all, delete-orphan"
    )


class ImportRow(UUIDPk, CreatedAt, Base):
    __tablename__ = "import_rows"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    row_number: Mapped[int] = mapped_column(Integer)  # 1-based, as the user sees it in Excel
    raw: Mapped[list] = mapped_column(JSONB)
    parsed: Mapped[dict] = mapped_column(JSONB)
    issues: Mapped[list] = mapped_column(JSONB, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ImportRowStatus] = mapped_column(str_enum(ImportRowStatus))
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")
