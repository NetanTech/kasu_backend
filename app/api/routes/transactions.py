import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import BusinessContext, get_business, require
from app.core.db import get_db
from app.models import Counterparty, Product, Transaction, TransactionEvent
from app.models.enums import (
    CounterpartyKind,
    Permission,
    TransactionKind,
    TransactionSource,
    TransactionStatus,
)
from app.schemas.transactions import (
    TransactionCorrect,
    TransactionCreate,
    TransactionEventOut,
    TransactionOut,
    TransactionPage,
    VoidIn,
)
from app.services import ledger
from app.services.classification import normalize_category
from app.services.memory import resolve_counterparty, resolve_product
from app.services.normalization import to_minor

router = APIRouter(prefix="/businesses/{business_id}/transactions", tags=["transactions"])

_CUSTOMER_KINDS = (TransactionKind.SALE, TransactionKind.CUSTOMER_PAYMENT)
_SUPPLIER_KINDS = (
    TransactionKind.PURCHASE,
    TransactionKind.EXPENSE,
    TransactionKind.SUPPLIER_PAYMENT,
)


def _out(txn: Transaction) -> TransactionOut:
    out = TransactionOut.model_validate(txn)
    out.product_name = txn.product.name if txn.product else None
    out.counterparty_name = txn.counterparty.name if txn.counterparty else None
    return out


# The owner's personal money movements. Staff without VIEW_PERSONAL never see these.
PERSONAL_KINDS = (
    TransactionKind.PERSONAL_EXPENSE,
    TransactionKind.OWNER_WITHDRAWAL,
    TransactionKind.OWNER_CONTRIBUTION,
)


def _visible(stmt, ctx: BusinessContext):
    if not ctx.can(Permission.VIEW_PERSONAL):
        stmt = stmt.where(Transaction.kind.not_in(PERSONAL_KINDS))
    return stmt


def _get(
    db: Session, ctx: BusinessContext, txn_id: uuid.UUID, *, own_record: bool = False
) -> Transaction:
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.product), selectinload(Transaction.counterparty))
        .where(Transaction.id == txn_id, Transaction.business_id == ctx.id)
    )
    # Staff can record e.g. "owner took ₦5,000 from the till" and get it back once, but
    # can't browse, correct or void the owner's personal records afterwards.
    txn = db.scalar(stmt if own_record else _visible(stmt, ctx))
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return txn


def _check_product(db: Session, ctx: BusinessContext, product_id: uuid.UUID) -> None:
    if not db.scalar(
        select(Product.id).where(Product.id == product_id, Product.business_id == ctx.id)
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown product_id")


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def create_transaction(
    body: TransactionCreate,
    response: Response,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> TransactionOut:
    if body.client_ref:
        existing = ledger.find_existing_by_client_ref(db, ctx.id, body.client_ref)
        if existing:
            response.status_code = status.HTTP_200_OK
            return _out(existing)

    product_id = body.product_id
    if product_id:
        _check_product(db, ctx, product_id)
    elif body.product_name:
        res = resolve_product(db, ctx.id, body.product_name, force_new=body.product_is_new)
        if res.entity is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "message": res.issue,
                    "suggestion": res.suggestion,
                    "hint": "Send product_id for the existing product, or product_is_new=true",
                },
            )
        product_id = res.entity.id

    counterparty_id = None
    if body.counterparty_name:
        kind = (
            CounterpartyKind.CUSTOMER
            if body.kind in _CUSTOMER_KINDS
            else CounterpartyKind.SUPPLIER
            if body.kind in _SUPPLIER_KINDS
            else None
        )
        if kind:
            counterparty_id = resolve_counterparty(db, ctx.id, kind, body.counterparty_name).id

    draft = ledger.TransactionDraft(
        kind=body.kind,
        occurred_on=body.occurred_on,
        amount_minor=to_minor(body.amount),
        currency=ctx.business.currency,
        settlement=body.settlement,
        quantity=body.quantity,
        unit=body.unit,
        product_id=product_id,
        counterparty_id=counterparty_id,
        category=normalize_category(body.category),
        description=body.description,
        source=TransactionSource.MANUAL,
        client_ref=body.client_ref,
    )
    try:
        txn = ledger.create_transaction(db, ctx.id, draft, ctx.user.id)
        db.commit()
    except ledger.LedgerError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except IntegrityError:
        # Concurrent retry with the same client_ref: return the winner.
        db.rollback()
        existing = ledger.find_existing_by_client_ref(db, ctx.id, body.client_ref or "")
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return _out(existing)
    return _out(_get(db, ctx, txn.id, own_record=True))


@router.get("", response_model=TransactionPage)
def list_transactions(
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
    start: date | None = None,
    end: date | None = None,
    kind: list[TransactionKind] | None = Query(default=None),
    status_: list[TransactionStatus] | None = Query(default=None, alias="status"),
    product_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> TransactionPage:
    stmt = _visible(select(Transaction).where(Transaction.business_id == ctx.id), ctx)
    if start:
        stmt = stmt.where(Transaction.occurred_on >= start)
    if end:
        stmt = stmt.where(Transaction.occurred_on <= end)
    if kind:
        stmt = stmt.where(Transaction.kind.in_(kind))
    if status_:
        stmt = stmt.where(Transaction.status.in_(status_))
    else:
        stmt = stmt.where(Transaction.status != TransactionStatus.VOIDED)
    if product_id:
        stmt = stmt.where(Transaction.product_id == product_id)
    if q:
        like = f"%{q}%"
        stmt = (
            stmt.outerjoin(Product)
            .outerjoin(Counterparty)
            .where(
                or_(
                    Transaction.description.ilike(like),
                    Transaction.category.ilike(like),
                    Product.name.ilike(like),
                    Counterparty.name.ilike(like),
                )
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(
        stmt.options(selectinload(Transaction.product), selectinload(Transaction.counterparty))
        .order_by(Transaction.occurred_on.desc(), Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return TransactionPage(items=[_out(t) for t in items], total=total, limit=limit, offset=offset)


@router.get("/{transaction_id}", response_model=TransactionOut)
def get_transaction(
    transaction_id: uuid.UUID,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> TransactionOut:
    return _out(_get(db, ctx, transaction_id))


@router.patch("/{transaction_id}", response_model=TransactionOut)
def correct_transaction(
    transaction_id: uuid.UUID,
    body: TransactionCorrect,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> TransactionOut:
    """Correct a transaction. The previous version is preserved in its event history."""
    txn = _get(db, ctx, transaction_id)
    updates = body.model_dump(exclude_unset=True)
    reason = updates.pop("reason", None)
    if "amount" in updates:
        amount = updates.pop("amount")
        if amount is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Amount cannot be empty")
        updates["amount_minor"] = to_minor(amount)
    for required in ("kind", "occurred_on", "settlement"):
        if required in updates and updates[required] is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{required} cannot be empty"
            )
    if updates.get("product_id"):
        _check_product(db, ctx, updates["product_id"])
    if "category" in updates:
        updates["category"] = normalize_category(updates["category"])
    try:
        ledger.correct_transaction(db, txn, updates, ctx.user.id, reason)
        db.commit()
    except ledger.LedgerError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    db.expire(txn)
    return _out(_get(db, ctx, transaction_id))


@router.post("/{transaction_id}/confirm", response_model=TransactionOut)
def confirm_transaction(
    transaction_id: uuid.UUID,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> TransactionOut:
    txn = _get(db, ctx, transaction_id)
    try:
        ledger.confirm_transaction(db, txn, ctx.user.id)
        db.commit()
    except ledger.LedgerError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return _out(txn)


@router.post("/{transaction_id}/void", response_model=TransactionOut)
def void_transaction(
    transaction_id: uuid.UUID,
    body: VoidIn,
    ctx: BusinessContext = Depends(require(Permission.RECORD)),
    db: Session = Depends(get_db),
) -> TransactionOut:
    """Retract a transaction. It stays in the audit trail but no longer counts anywhere."""
    txn = _get(db, ctx, transaction_id)
    try:
        ledger.void_transaction(db, txn, ctx.user.id, body.reason)
        db.commit()
    except ledger.LedgerError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return _out(txn)


@router.get("/{transaction_id}/history", response_model=list[TransactionEventOut])
def transaction_history(
    transaction_id: uuid.UUID,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> list[TransactionEvent]:
    _get(db, ctx, transaction_id)
    return list(
        db.scalars(
            select(TransactionEvent)
            .where(
                TransactionEvent.transaction_id == transaction_id,
                TransactionEvent.business_id == ctx.id,
            )
            .order_by(TransactionEvent.version)
        )
    )
