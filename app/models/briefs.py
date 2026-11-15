"""Stored weekly briefs and in-app notifications (KASU-030)."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk, str_enum
from app.models.enums import BriefStatus, NotificationKind


class WeeklyBrief(UUIDPk, CreatedAt, Base):
    """One generated brief per business per week. `content` is the brief exactly as it was
    when generated, so later corrections don't silently rewrite what people were told."""

    __tablename__ = "weekly_briefs"
    __table_args__ = (UniqueConstraint("business_id", "week_end"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    status: Mapped[BriefStatus] = mapped_column(str_enum(BriefStatus))
    content: Mapped[dict] = mapped_column(JSONB)


class Notification(UUIDPk, CreatedAt, Base):
    """An in-app notification for one person. `opened_at` is set the first time they open
    what it points to; for weekly briefs that is what the open rate is measured from."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_created", "user_id", "created_at"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[NotificationKind] = mapped_column(str_enum(NotificationKind))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    brief_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("weekly_briefs.id", ondelete="CASCADE"), index=True
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
