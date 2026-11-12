"""Resolve free-text product/customer/supplier names against Business Memory."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Counterparty, Product, ProductAlias
from app.models.enums import CounterpartyKind
from app.services.normalization import normalize_key, similarity

# Names this similar to an existing product but not identical are ambiguous: ask, don't guess.
AMBIGUOUS_SIMILARITY = 0.82


@dataclass
class Resolution[T]:
    entity: T | None
    created: bool = False
    issue: str | None = None
    suggestion: str | None = None  # name of the existing entity we think it might be


def resolve_product(
    db: Session,
    business_id: uuid.UUID,
    name: str,
    *,
    create: bool = True,
    force_new: bool = False,
) -> Resolution[Product]:
    """Exact alias match -> that product. Near-miss -> ambiguity issue (unless the user said
    it's a new product via force_new). Otherwise create a new product if allowed."""
    key = normalize_key(name)
    if not key:
        return Resolution(None)

    alias = db.scalar(
        select(ProductAlias).where(
            ProductAlias.business_id == business_id, ProductAlias.alias_key == key
        )
    )
    if alias:
        return Resolution(alias.product)

    products = db.scalars(select(Product).where(Product.business_id == business_id)).all()
    best = max(products, key=lambda p: similarity(p.name, name), default=None)
    if not force_new and best and similarity(best.name, name) >= AMBIGUOUS_SIMILARITY:
        return Resolution(
            None,
            issue=f"'{name}' looks like existing product '{best.name}'. Is it the same product?",
            suggestion=best.name,
        )

    if not create:
        return Resolution(None, issue=f"Unknown product '{name}'")

    product = Product(business_id=business_id, name=name.strip())
    product.aliases.append(ProductAlias(business_id=business_id, alias_key=key))
    db.add(product)
    db.flush()
    return Resolution(product, created=True)


class AliasConflict(Exception):
    pass


def add_product_alias(db: Session, product: Product, alias: str) -> None:
    key = normalize_key(alias)
    exists = db.scalar(
        select(ProductAlias).where(
            ProductAlias.business_id == product.business_id, ProductAlias.alias_key == key
        )
    )
    if exists is not None and exists.product_id != product.id:
        raise AliasConflict(f"'{alias}' already refers to another product")
    if exists is None:
        db.add(ProductAlias(business_id=product.business_id, product_id=product.id, alias_key=key))
        db.flush()


def resolve_counterparty(
    db: Session, business_id: uuid.UUID, kind: CounterpartyKind, name: str
) -> Counterparty | None:
    key = normalize_key(name)
    if not key:
        return None
    existing = db.scalar(
        select(Counterparty).where(
            Counterparty.business_id == business_id,
            Counterparty.kind == kind,
            Counterparty.name_key == key,
        )
    )
    if existing:
        return existing
    cp = Counterparty(business_id=business_id, kind=kind, name=name.strip(), name_key=key)
    db.add(cp)
    db.flush()
    return cp
