import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import BusinessContext, get_business, get_current_user, require_admin
from app.core.db import get_db
from app.models import Business, BusinessMember, Product, ProductAlias, User
from app.models.enums import MemberRole
from app.schemas.business import (
    AliasIn,
    BusinessCreate,
    BusinessOut,
    BusinessUpdate,
    ProductOut,
    ProductUpdate,
)
from app.services.memory import AliasConflict, add_product_alias

router = APIRouter(prefix="/businesses", tags=["businesses"])


def _out(business: Business, role: MemberRole) -> BusinessOut:
    return BusinessOut.model_validate({**business.__dict__, "role": role})


@router.post("", response_model=BusinessOut, status_code=status.HTTP_201_CREATED)
def create_business(
    body: BusinessCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BusinessOut:
    business = Business(
        name=body.name.strip(),
        sector=body.sector,
        country=body.country.upper(),
        currency=body.currency.upper(),
        created_by=user.id,
    )
    db.add(business)
    db.flush()
    db.add(BusinessMember(business_id=business.id, user_id=user.id, role=MemberRole.OWNER))
    db.commit()
    return _out(business, MemberRole.OWNER)


@router.get("", response_model=list[BusinessOut])
def list_businesses(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[BusinessOut]:
    rows = db.execute(
        select(Business, BusinessMember.role)
        .join(BusinessMember, BusinessMember.business_id == Business.id)
        .where(BusinessMember.user_id == user.id)
        .order_by(Business.created_at)
    ).all()
    return [_out(b, role) for b, role in rows]


@router.get("/{business_id}", response_model=BusinessOut)
def get_business_detail(ctx: BusinessContext = Depends(get_business)) -> BusinessOut:
    return _out(ctx.business, ctx.role)


@router.patch("/{business_id}", response_model=BusinessOut)
def update_business(
    body: BusinessUpdate,
    ctx: BusinessContext = Depends(require_admin),
    db: Session = Depends(get_db),
) -> BusinessOut:
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(ctx.business, name, value)
    db.commit()
    return _out(ctx.business, ctx.role)


# ----------------------------------------------------------------- products (Business Memory)


@router.get("/{business_id}/products", response_model=list[ProductOut])
def list_products(
    ctx: BusinessContext = Depends(get_business), db: Session = Depends(get_db)
) -> list[Product]:
    return list(
        db.scalars(select(Product).where(Product.business_id == ctx.id).order_by(Product.name))
    )


def _product(db: Session, ctx: BusinessContext, product_id: uuid.UUID) -> Product:
    product = db.scalar(
        select(Product).where(Product.id == product_id, Product.business_id == ctx.id)
    )
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@router.patch("/{business_id}/products/{product_id}", response_model=ProductOut)
def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> Product:
    product = _product(db, ctx, product_id)
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(product, name, value)
    if body.name:
        try:
            add_product_alias(db, product, body.name)
        except AliasConflict as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.commit()
    return product


@router.post(
    "/{business_id}/products/{product_id}/aliases",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
def add_alias(
    product_id: uuid.UUID,
    body: AliasIn,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> list[str]:
    """Teach KASU that another spelling ("one bag rice") means this product."""
    product = _product(db, ctx, product_id)
    try:
        add_product_alias(db, product, body.alias)
        db.commit()
    except (AliasConflict, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return list(
        db.scalars(select(ProductAlias.alias_key).where(ProductAlias.product_id == product.id))
    )
