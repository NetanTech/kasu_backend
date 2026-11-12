import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk, str_enum
from app.models.enums import MemberRole


class User(UUIDPk, CreatedAt, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    memberships: Mapped[list["BusinessMember"]] = relationship(back_populates="user")


class Business(UUIDPk, CreatedAt, Base):
    """A tenant. Every business-owned row carries business_id (PRD §32)."""

    __tablename__ = "businesses"

    name: Mapped[str] = mapped_column(String(200))
    sector: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2), default="NG")
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))

    members: Mapped[list["BusinessMember"]] = relationship(back_populates="business")


class BusinessMember(UUIDPk, CreatedAt, Base):
    __tablename__ = "business_members"
    __table_args__ = (UniqueConstraint("business_id", "user_id"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MemberRole] = mapped_column(str_enum(MemberRole), default=MemberRole.OWNER)

    business: Mapped[Business] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")
