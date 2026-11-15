"""Security log: every rejected access attempt (KASU-002)."""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk


class AccessDenial(UUIDPk, CreatedAt, Base):
    """A request that was refused because the caller wasn't allowed to make it.

    `business_id` has no foreign key on purpose: probes often target businesses that don't
    exist, and the record must survive the business being deleted.
    """

    __tablename__ = "access_denials"
    __table_args__ = (Index("ix_access_denials_business_created", "business_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    business_id: Mapped[uuid.UUID | None]
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    status_code: Mapped[int] = mapped_column(Integer)
    # missing_token | invalid_token | inactive_user | login_failed | not_a_member |
    # missing_permission | team_rule
    reason: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
