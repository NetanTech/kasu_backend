"""Import workflow: upload -> stage rows for review -> user corrections -> commit to ledger."""

import uuid
from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, ImportBatch, ImportRow
from app.models.base import utcnow
from app.models.enums import (
    CounterpartyKind,
    ImportRowStatus,
    ImportStatus,
    Settlement,
    TransactionKind,
    TransactionSource,
    TransactionStatus,
)
from app.services import ledger
from app.services.classification import normalize_category
from app.services.imports.detect import detect_header_row, map_columns
from app.services.imports.reader import UnsupportedFile, pick_sheet, read_file
from app.services.imports.rows import (
    REVIEW_THRESHOLD,
    extract_row,
    is_blank,
    row_confidence,
    validate_parsed,
)
from app.services.memory import resolve_counterparty, resolve_product
from app.services.normalization import parse_amount, parse_date, parse_quantity, to_minor
from app.services.storage import save_upload

PRODUCT_KINDS = (TransactionKind.SALE, TransactionKind.PURCHASE)

EDITABLE_ROW_FIELDS = (
    "occurred_on",
    "kind",
    "amount",
    "quantity",
    "product",
    "description",
    "category",
    "customer",
    "supplier",
    "settlement",
)


class ImportError_(Exception):
    pass


def _clean_edit(name: str, value: Any) -> Any:
    """Normalize a user's edit to a staged row into the same form `extract_row` produces."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        match name:
            case "occurred_on":
                parsed = parse_date(value)
                if parsed.value is None:
                    raise ValueError(parsed.issue or "Invalid date")
                return parsed.value.isoformat()
            case "amount":
                parsed = parse_amount(value)
                if parsed.value is None:
                    raise ValueError(parsed.issue or "Invalid amount")
                return str(parsed.value)
            case "quantity":
                parsed = parse_quantity(value)
                if parsed.value is None:
                    raise ValueError(parsed.issue or "Invalid quantity")
                return str(parsed.value)
            case "kind":
                return TransactionKind(value).value
            case "settlement":
                return Settlement(value).value
            case "category":
                return normalize_category(str(value))
            case _:
                return str(value).strip()[:500]
    except ValueError as exc:
        raise ImportError_(f"{name}: {exc}") from None


def _product_id_if_known(db: Session, business_id: uuid.UUID, parsed: dict) -> uuid.UUID | None:
    if parsed.get("kind") not in PRODUCT_KINDS or not parsed.get("product"):
        return None
    res = resolve_product(db, business_id, parsed["product"], create=False)
    return res.entity.id if res.entity else None


def _ledger_description(parsed: dict, product_id: uuid.UUID | None) -> str | None:
    """The description the committed transaction will carry. Staging and commit must agree on
    this, otherwise duplicate detection compares different keys."""
    description = parsed.get("description")
    if parsed.get("product") and product_id is None:
        return description or parsed["product"]
    return description


def _evaluate(
    db: Session, business_id: uuid.UUID, row: ImportRow, seen: Counter | None = None
) -> None:
    """(Re)compute issues, confidence, status for one staged row. Never touches the ledger."""
    if row.status in (ImportRowStatus.SKIPPED, ImportRowStatus.COMMITTED):
        return
    parsed = row.parsed
    issues = validate_parsed(parsed)

    # Product: ambiguous names need the user's decision (PRD §9).
    if (
        parsed.get("kind") in PRODUCT_KINDS
        and parsed.get("product")
        and not parsed.get("product_is_new")
    ):
        res = resolve_product(db, business_id, parsed["product"], create=False)
        if res.suggestion:
            issues.append(
                {
                    "field": "product",
                    "message": res.issue,
                    "blocking": False,
                    "suggestion": res.suggestion,
                }
            )

    status = ImportRowStatus.READY
    if not any(i["blocking"] for i in issues):
        product_id = _product_id_if_known(db, business_id, parsed)
        key = ledger.dedupe_key(
            TransactionKind(parsed["kind"]),
            date.fromisoformat(parsed["occurred_on"]),
            to_minor(Decimal(parsed["amount"])),
            product_id,
            _ledger_description(parsed, product_id),
        )
        parsed_key_seen = seen[key] if seen is not None else 0
        if seen is not None:
            seen[key] += 1
        if not parsed.get("allow_duplicate"):
            if ledger.find_duplicates(db, business_id, key):
                status = ImportRowStatus.DUPLICATE
                issues.append(
                    {
                        "field": None,
                        "message": "This transaction already exists in your records",
                        "blocking": False,
                    }
                )
            elif parsed_key_seen:
                issues.append(
                    {
                        "field": None,
                        "message": "Same as another row in this file. Keep both?",
                        "blocking": False,
                    }
                )

    confidence = row_confidence(parsed, issues)
    if status != ImportRowStatus.DUPLICATE and issues:
        status = ImportRowStatus.NEEDS_REVIEW
    row.issues = issues
    row.confidence = confidence
    row.status = status


def _stats(batch: ImportBatch) -> dict[str, Any]:
    counts = Counter(r.status.value for r in batch.rows)
    return {
        "total_rows": len(batch.rows),
        **{s.value: counts.get(s.value, 0) for s in ImportRowStatus},
    }


def _stage_rows(db: Session, batch: ImportBatch, rows: list[list[Any]], default_kind) -> None:
    batch.rows.clear()
    db.flush()
    mapping = batch.column_mapping or {}
    seen: Counter = Counter()
    start = (batch.header_row + 1) if batch.header_row is not None else 0
    for offset, cells in enumerate(rows[start:]):
        if is_blank(cells):
            continue
        row = ImportRow(
            business_id=batch.business_id,
            row_number=start + offset + 1,
            raw=[None if c is None else str(c) for c in cells],
            parsed=extract_row(cells, mapping, default_kind),
            issues=[],
            status=ImportRowStatus.NEEDS_REVIEW,
        )
        batch.rows.append(row)
        _evaluate(db, batch.business_id, row, seen)
    batch.stats = _stats(batch)


def create_import(
    db: Session,
    business: Business,
    user_id: uuid.UUID,
    filename: str,
    content_type: str | None,
    data: bytes,
    sheet_name: str | None = None,
    default_kind: TransactionKind | None = None,
) -> ImportBatch:
    sheets = read_file(filename, data)
    sheet = pick_sheet(sheets, sheet_name)
    header_row = detect_header_row(sheet.rows)
    headers = sheet.rows[header_row] if header_row is not None else []
    mapping = map_columns(headers)

    storage_path, digest = save_upload(business.id, filename, data)
    previous = db.scalar(
        select(ImportBatch).where(
            ImportBatch.business_id == business.id,
            ImportBatch.file_sha256 == digest,
            ImportBatch.status == ImportStatus.COMMITTED,
        )
    )

    batch = ImportBatch(
        business_id=business.id,
        uploaded_by=user_id,
        filename=filename,
        content_type=content_type,
        storage_path=storage_path,
        file_sha256=digest,
        sheet_name=sheet.name,
        header_row=header_row,
        headers=[None if h is None else str(h) for h in headers],
        column_mapping=mapping,
        status=ImportStatus.NEEDS_REVIEW,
    )
    db.add(batch)
    db.flush()

    if header_row is None or not ({"amount", "money_in", "money_out"} & mapping.keys()):
        batch.error = (
            "Couldn't find the column headers (we need at least a date and an amount). "
            "Set the column mapping and we'll re-read the file."
        )
    _stage_rows(db, batch, sheet.rows, default_kind)
    batch.stats = {
        **batch.stats,
        "sheets": [s.name for s in sheets],
        "previously_imported_batch_id": str(previous.id) if previous else None,
        "default_kind": str(default_kind) if default_kind else None,
    }
    return batch


def remap_import(
    db: Session,
    batch: ImportBatch,
    mapping: dict[str, int],
    header_row: int | None,
    default_kind: TransactionKind | None,
) -> ImportBatch:
    """User fixed the column mapping / header row: re-read the stored file and re-stage."""
    if batch.status != ImportStatus.NEEDS_REVIEW:
        raise ImportError_("Only imports awaiting review can be re-mapped")
    with open(batch.storage_path, "rb") as fh:
        data = fh.read()
    sheet = pick_sheet(read_file(batch.filename, data), batch.sheet_name)
    batch.header_row = header_row
    batch.headers = (
        [None if h is None else str(h) for h in sheet.rows[header_row]]
        if header_row is not None and header_row < len(sheet.rows)
        else []
    )
    batch.column_mapping = mapping
    batch.error = None
    stats_extra = {
        k: v
        for k, v in (batch.stats or {}).items()
        if k in ("sheets", "previously_imported_batch_id")
    }
    _stage_rows(db, batch, sheet.rows, default_kind)
    batch.stats = {
        **batch.stats,
        **stats_extra,
        "default_kind": str(default_kind) if default_kind else None,
    }
    return batch


def update_row(
    db: Session,
    batch: ImportBatch,
    row: ImportRow,
    changes: dict[str, Any],
    skip: bool | None,
    product_is_new: bool | None,
    allow_duplicate: bool | None,
) -> ImportRow:
    if batch.status != ImportStatus.NEEDS_REVIEW:
        raise ImportError_("This import has already been committed")
    if skip is True:
        row.status = ImportRowStatus.SKIPPED
        batch.stats = {**(batch.stats or {}), **_stats(batch)}
        return row
    if skip is False and row.status == ImportRowStatus.SKIPPED:
        row.status = ImportRowStatus.NEEDS_REVIEW

    parsed = dict(row.parsed)
    conf = dict(parsed.get("field_confidence", {}))
    errors = dict(parsed.get("parse_errors", {}))
    for name, value in changes.items():
        if name not in EDITABLE_ROW_FIELDS:
            raise ImportError_(f"Field '{name}' cannot be edited")
        parsed[name] = _clean_edit(name, value)
        conf[name] = 1.0  # user-provided
        errors.pop(name, None)
    if product_is_new is not None:
        parsed["product_is_new"] = product_is_new
    if allow_duplicate is not None:
        parsed["allow_duplicate"] = allow_duplicate
    parsed["field_confidence"] = conf
    parsed["parse_errors"] = errors
    row.parsed = parsed  # reassign so SQLAlchemy sees the JSONB change

    _evaluate(db, batch.business_id, row)
    batch.stats = {**(batch.stats or {}), **_stats(batch)}
    return row


def _draft_from_row(
    db: Session, business: Business, row: ImportRow, provisional: bool
) -> ledger.TransactionDraft:
    p = row.parsed
    kind = TransactionKind(p["kind"])
    product_id = None
    if kind in PRODUCT_KINDS and p.get("product"):
        res = resolve_product(
            db, business.id, p["product"], create=True, force_new=bool(p.get("product_is_new"))
        )
        # Ambiguous product we weren't told how to resolve: don't guess; keep it as text.
        product_id = res.entity.id if res.entity else None
    description = _ledger_description(p, product_id)

    counterparty_id = None
    if p.get("customer") and kind in (TransactionKind.SALE, TransactionKind.CUSTOMER_PAYMENT):
        counterparty_id = resolve_counterparty(
            db, business.id, CounterpartyKind.CUSTOMER, p["customer"]
        ).id
    elif p.get("supplier") and kind in (
        TransactionKind.PURCHASE,
        TransactionKind.EXPENSE,
        TransactionKind.SUPPLIER_PAYMENT,
    ):
        counterparty_id = resolve_counterparty(
            db, business.id, CounterpartyKind.SUPPLIER, p["supplier"]
        ).id

    return ledger.TransactionDraft(
        kind=kind,
        occurred_on=date.fromisoformat(p["occurred_on"]),
        amount_minor=to_minor(Decimal(p["amount"])),
        currency=business.currency,
        settlement=Settlement(p.get("settlement") or Settlement.PAID),
        quantity=Decimal(p["quantity"]) if p.get("quantity") else None,
        product_id=product_id,
        counterparty_id=counterparty_id,
        category=p.get("category") if kind == TransactionKind.EXPENSE else None,
        description=description,
        source=TransactionSource.IMPORT,
        source_ref=f"import_row:{row.id}",
        confidence=row.confidence if provisional else max(row.confidence, REVIEW_THRESHOLD),
        status=TransactionStatus.PROVISIONAL if provisional else TransactionStatus.CONFIRMED,
    )


def commit_import(
    db: Session,
    business: Business,
    batch: ImportBatch,
    user_id: uuid.UUID,
    keep_unreviewed_as_provisional: bool = True,
) -> ImportBatch:
    """Write staged rows to the ledger.

    READY rows -> confirmed transactions. NEEDS_REVIEW rows without blocking problems ->
    provisional transactions (kept out of high-confidence analytics until confirmed).
    Rows with blocking problems -> UNRESOLVED (no transaction; PRD §7.1).
    DUPLICATE / SKIPPED rows are left alone.
    """
    if batch.status != ImportStatus.NEEDS_REVIEW:
        raise ImportError_("This import has already been committed")

    for row in batch.rows:
        if row.status == ImportRowStatus.READY:
            provisional = False
        elif row.status == ImportRowStatus.NEEDS_REVIEW:
            if any(i["blocking"] for i in row.issues) or not keep_unreviewed_as_provisional:
                row.status = ImportRowStatus.UNRESOLVED
                continue
            provisional = True
        else:
            continue
        txn = ledger.create_transaction(
            db, business.id, _draft_from_row(db, business, row, provisional), user_id
        )
        row.transaction_id = txn.id
        row.status = ImportRowStatus.COMMITTED

    batch.status = ImportStatus.COMMITTED
    batch.committed_at = utcnow()
    batch.stats = {**(batch.stats or {}), **_stats(batch)}
    return batch


__all__ = [
    "ImportError_",
    "UnsupportedFile",
    "commit_import",
    "create_import",
    "remap_import",
    "update_row",
]
