"""Team invitations and the business audit log."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk, str_enum
from app.models.enums import MemberRole
from app.models.tenancy import Business, User


class Invitation(UUIDPk, CreatedAt, Base):
    """An invite to join a business. Only a hash of the token is stored; the token itself is
    shown once, to the person who created the invite, as a shareable link."""

    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitations_business_email", "business_id", "email"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[MemberRole] = mapped_column(str_enum(MemberRole))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship()
    inviter: Mapped[User | None] = relationship(foreign_keys=[invited_by])


class AuditEvent(UUIDPk, CreatedAt, Base):
    """Who did what to the business's team and settings (PRD §31)."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_business_created", "business_id", "created_at"),)

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64))  # e.g. "member.role_changed"
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
