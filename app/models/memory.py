"""Business Memory entities: products and counterparties, with alias-based resolution (PRD §8–9)."""

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import CreatedAt, UUIDPk, str_enum
from app.models.enums import CounterpartyKind


class Product(UUIDPk, CreatedAt, Base):
    __tablename__ = "products"

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str | None] = mapped_column(String(50))  # e.g. "bag", "kg", "piece"
    category: Mapped[str | None] = mapped_column(String(100))

    aliases: Mapped[list["ProductAlias"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductAlias(UUIDPk, CreatedAt, Base):
    """Every spelling we've seen for a product ("RICE", "rice 50kg") points at one Product."""

    __tablename__ = "product_aliases"
    __table_args__ = (UniqueConstraint("business_id", "alias_key"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    alias_key: Mapped[str] = mapped_column(String(200))

    product: Mapped[Product] = relationship(back_populates="aliases")


class Counterparty(UUIDPk, CreatedAt, Base):
    """A customer or supplier."""

    __tablename__ = "counterparties"
    __table_args__ = (UniqueConstraint("business_id", "kind", "name_key"),)

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[CounterpartyKind] = mapped_column(str_enum(CounterpartyKind))
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
