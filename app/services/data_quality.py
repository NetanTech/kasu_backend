"""Live data-quality tracking (KASU-018).

* Unresolved-record rate: of the rows people tried to import, how many could not become a
  transaction at all; plus how many recorded transactions are still provisional (unconfirmed),
  which keeps them out of the numbers.
* Critical-field error rate: of the transactions recorded, how many later had a critical field
  (kind, date, amount, quantity, product) corrected by a person. Every correction is in the
  append-only event log, so this is measured from what users actually fixed, by source.

Records are counted by when they entered KASU. Recent ones have had less time to be corrected,
so the error rate for the last few days is a lower bound.
"""

import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ImportRow, Transaction, TransactionEvent
from app.models.enums import ImportRowStatus, TransactionEventType, TransactionStatus

CRITICAL_FIELDS = ("kind", "occurred_on", "amount_minor", "quantity", "product_id")


def _pct(part: int, whole: int) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(part) / whole * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)


def data_quality(db: Session, business_id: uuid.UUID | None, start: date, end: date) -> dict:
    """Platform-wide when `business_id` is None."""
    lo = datetime.combine(start, time.min, UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, UTC)

    # --- imports
    row_stmt = (
        select(ImportRow.status, func.count())
        .where(ImportRow.created_at >= lo, ImportRow.created_at < hi)
        .group_by(ImportRow.status)
    )
    if business_id:
        row_stmt = row_stmt.where(ImportRow.business_id == business_id)
    rows = {status.value: n for status, n in db.execute(row_stmt)}
    rows_total = sum(rows.values())
    unresolved = rows.get(ImportRowStatus.UNRESOLVED.value, 0)

    # --- transactions and their corrections
    txn_stmt = select(Transaction.id, Transaction.source, Transaction.status).where(
        Transaction.created_at >= lo, Transaction.created_at < hi
    )
    if business_id:
        txn_stmt = txn_stmt.where(Transaction.business_id == business_id)
    txns = db.execute(txn_stmt).all()
    source_of = {t.id: t.source.value for t in txns}
    provisional = sum(1 for t in txns if t.status == TransactionStatus.PROVISIONAL)

    corrected_fields: dict[uuid.UUID, set[str]] = defaultdict(set)
    if source_of:
        events = db.execute(
            select(TransactionEvent.transaction_id, TransactionEvent.changes).where(
                TransactionEvent.event_type == TransactionEventType.CORRECTED,
                TransactionEvent.transaction_id.in_(list(source_of)),
            )
        )
        for txn_id, changes in events:
            hit = set(changes or {}) & set(CRITICAL_FIELDS)
            if hit:
                corrected_fields[txn_id] |= hit

    by_field = Counter(f for fields in corrected_fields.values() for f in fields)
    by_source_total = Counter(source_of.values())
    by_source_err = Counter(source_of[t] for t in corrected_fields)

    return {
        "period": {"start": start, "end": end},
        "unresolved_records": {
            "import_rows": rows_total,
            "unresolved": unresolved,
            "unresolved_rate_pct": _pct(unresolved, rows_total),
            "import_rows_by_status": rows,
            "transactions": len(txns),
            "provisional": provisional,
            "provisional_rate_pct": _pct(provisional, len(txns)),
        },
        "critical_field_errors": {
            "transactions": len(txns),
            "corrected": len(corrected_fields),
            "error_rate_pct": _pct(len(corrected_fields), len(txns)),
            "by_field": {f: by_field.get(f, 0) for f in CRITICAL_FIELDS},
            "by_source": {
                src: {
                    "transactions": n,
                    "corrected": by_source_err.get(src, 0),
                    "error_rate_pct": _pct(by_source_err.get(src, 0), n),
                }
                for src, n in sorted(by_source_total.items())
            },
        },
        "note": (
            "Counts records by when they entered KASU. Errors are corrections people made to "
            "kind, date, amount, quantity or product; recent records have had less time to be "
            "corrected."
        ),
    }
