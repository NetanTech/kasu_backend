"""All writes to the ledger go through here so every change produces an audit event (PRD §20)."""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction, TransactionEvent
from app.models.base import utcnow
from app.models.enums import (
    Settlement,
    TransactionEventType,
    TransactionKind,
    TransactionSource,
    TransactionStatus,
)
from app.services.normalization import normalize_key

# Fields a user may correct. Anything else (ids, source, version...) is system-owned.
CORRECTABLE_FIELDS = (
    "kind",
    "settlement",
    "occurred_on",
    "amount_minor",
    "quantity",
    "unit",
    "product_id",
    "counterparty_id",
    "category",
    "description",
)

SNAPSHOT_FIELDS = (*CORRECTABLE_FIELDS, "status", "currency", "confidence", "source", "source_ref")


class LedgerError(Exception):
    pass


@dataclass
class TransactionDraft:
    kind: TransactionKind
    occurred_on: date
    amount_minor: int
    source: TransactionSource
    settlement: Settlement = Settlement.PAID
    currency: str = "NGN"
    quantity: Decimal | None = None
    unit: str | None = None
    product_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    category: str | None = None
    description: str | None = None
    source_ref: str | None = None
    client_ref: str | None = None
    confidence: float = 1.0
    status: TransactionStatus = TransactionStatus.CONFIRMED
    extra: dict[str, Any] = field(default_factory=dict)


def dedupe_key(
    kind: TransactionKind,
    occurred_on: date,
    amount_minor: int,
    product_id: uuid.UUID | None,
    description: str | None,
) -> str:
    basis = "|".join(
        [
            str(kind),
            occurred_on.isoformat(),
            str(amount_minor),
            str(product_id or ""),
            normalize_key(description or ""),
        ]
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID | date | Decimal):
        return str(value)
    return value


def snapshot(txn: Transaction) -> dict[str, Any]:
    return {f: _jsonable(getattr(txn, f)) for f in SNAPSHOT_FIELDS}


def _record_event(
    db: Session,
    txn: Transaction,
    event_type: TransactionEventType,
    actor: uuid.UUID | None,
    changes: dict[str, list[Any]] | None = None,
    reason: str | None = None,
) -> TransactionEvent:
    event = TransactionEvent(
        business_id=txn.business_id,
        transaction_id=txn.id,
        event_type=event_type,
        version=txn.version,
        snapshot=snapshot(txn),
        changes=changes,
        reason=reason,
        actor_user_id=actor,
    )
    db.add(event)
    return event


def find_existing_by_client_ref(
    db: Session, business_id: uuid.UUID, client_ref: str
) -> Transaction | None:
    return db.scalar(
        select(Transaction).where(
            Transaction.business_id == business_id, Transaction.client_ref == client_ref
        )
    )


def find_duplicates(
    db: Session, business_id: uuid.UUID, key: str, exclude_id: uuid.UUID | None = None
) -> list[Transaction]:
    stmt = select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.dedupe_key == key,
        Transaction.status != TransactionStatus.VOIDED,
    )
    if exclude_id:
        stmt = stmt.where(Transaction.id != exclude_id)
    return list(db.scalars(stmt))


def create_transaction(
    db: Session, business_id: uuid.UUID, draft: TransactionDraft, actor: uuid.UUID | None
) -> Transaction:
    if draft.amount_minor <= 0:
        raise LedgerError("Amount must be greater than zero")
    if draft.occurred_on > date.today() + timedelta(days=1):
        raise LedgerError("Transaction date cannot be in the future")

    txn = Transaction(
        id=uuid.uuid4(),
        business_id=business_id,
        kind=draft.kind,
        status=draft.status,
        settlement=draft.settlement,
        occurred_on=draft.occurred_on,
        amount_minor=draft.amount_minor,
        currency=draft.currency,
        quantity=draft.quantity,
        unit=draft.unit,
        product_id=draft.product_id,
        counterparty_id=draft.counterparty_id,
        category=draft.category,
        description=draft.description,
        source=draft.source,
        source_ref=draft.source_ref,
        client_ref=draft.client_ref,
        confidence=draft.confidence,
        dedupe_key=dedupe_key(
            draft.kind, draft.occurred_on, draft.amount_minor, draft.product_id, draft.description
        ),
        version=1,
        created_by=actor,
    )
    db.add(txn)
    db.flush()
    _record_event(db, txn, TransactionEventType.CREATED, actor)
    return txn


def correct_transaction(
    db: Session,
    txn: Transaction,
    updates: dict[str, Any],
    actor: uuid.UUID | None,
    reason: str | None = None,
) -> Transaction:
    if txn.status == TransactionStatus.VOIDED:
        raise LedgerError("A voided transaction cannot be corrected")
    unknown = set(updates) - set(CORRECTABLE_FIELDS)
    if unknown:
        raise LedgerError(f"Fields cannot be corrected: {', '.join(sorted(unknown))}")
    if "amount_minor" in updates and updates["amount_minor"] <= 0:
        raise LedgerError("Amount must be greater than zero")

    changes: dict[str, list[Any]] = {}
    for name, new in updates.items():
        old = getattr(txn, name)
        if old != new:
            changes[name] = [_jsonable(old), _jsonable(new)]
            setattr(txn, name, new)
    if not changes:
        return txn

    txn.dedupe_key = dedupe_key(
        txn.kind, txn.occurred_on, txn.amount_minor, txn.product_id, txn.description
    )
    # A human correcting a record is the strongest signal we have.
    txn.confidence = 1.0
    txn.version += 1
    txn.updated_at = utcnow()
    _record_event(db, txn, TransactionEventType.CORRECTED, actor, changes, reason)
    return txn


def confirm_transaction(db: Session, txn: Transaction, actor: uuid.UUID | None) -> Transaction:
    if txn.status != TransactionStatus.PROVISIONAL:
        raise LedgerError("Only provisional transactions can be confirmed")
    txn.status = TransactionStatus.CONFIRMED
    txn.confidence = 1.0
    txn.version += 1
    txn.updated_at = utcnow()
    _record_event(
        db,
        txn,
        TransactionEventType.CONFIRMED,
        actor,
        {"status": [TransactionStatus.PROVISIONAL.value, TransactionStatus.CONFIRMED.value]},
    )
    return txn


def void_transaction(
    db: Session, txn: Transaction, actor: uuid.UUID | None, reason: str | None = None
) -> Transaction:
    if txn.status == TransactionStatus.VOIDED:
        raise LedgerError("Transaction is already voided")
    old = txn.status
    txn.status = TransactionStatus.VOIDED
    txn.version += 1
    txn.updated_at = utcnow()
    _record_event(
        db,
        txn,
        TransactionEventType.VOIDED,
        actor,
        {"status": [old.value, TransactionStatus.VOIDED.value]},
        reason,
    )
    return txn
