"""Turn one spreadsheet row into a candidate transaction plus a list of issues.

`extract_row` reads raw cells -> `parsed` (a JSON-safe dict of canonical values + per-field
confidence). `validate_parsed` decides what's still wrong. Validation is separate so a user's
corrections to `parsed` can be re-validated without re-reading the file.
"""

from decimal import Decimal
from typing import Any

from app.models.enums import Settlement, TransactionKind
from app.services.classification import guess_category, guess_kind, normalize_category
from app.services.normalization import (
    normalize_key,
    parse_amount,
    parse_date,
    parse_quantity,
)

# Below this, a field is a guess the user should confirm before it's trusted (PRD §19).
REVIEW_THRESHOLD = 0.8

_CREDIT_WORDS = ("credit", "owing", "owe", "unpaid", "debt", "later", "not paid", "balance")


def _issue(field: str, message: str, blocking: bool) -> dict[str, Any]:
    return {"field": field, "message": message, "blocking": blocking}


def _cell(cells: list[Any], mapping: dict[str, int], field: str) -> Any:
    index = mapping.get(field)
    if index is None or index >= len(cells):
        return None
    value = cells[index]
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def is_blank(cells: list[Any]) -> bool:
    return all(c is None or (isinstance(c, str) and not c.strip()) for c in cells)


def extract_row(
    cells: list[Any], mapping: dict[str, int], default_kind: TransactionKind | None = None
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    conf: dict[str, float] = {}
    parse_errors: dict[str, str] = {}

    def text(field: str) -> str | None:
        value = _cell(cells, mapping, field)
        return None if value is None else str(value)

    # --- date
    d = parse_date(_cell(cells, mapping, "date"))
    parsed["occurred_on"] = d.value.isoformat() if d.value else None
    if d.issue:
        parse_errors["occurred_on"] = d.issue

    # --- descriptive text
    parsed["product"] = text("product")
    parsed["description"] = text("description")
    parsed["customer"] = text("customer")
    parsed["supplier"] = text("supplier")

    # --- quantity / unit price
    q = parse_quantity(_cell(cells, mapping, "quantity"))
    parsed["quantity"] = str(q.value) if q.value is not None else None
    if q.issue:
        parse_errors["quantity"] = q.issue
    up = parse_amount(_cell(cells, mapping, "unit_price"))
    if up.issue:
        parse_errors["unit_price"] = up.issue

    # --- amount + direction hint
    direction: TransactionKind | None = None
    amount = parse_amount(_cell(cells, mapping, "amount"))
    if amount.issue:
        parse_errors["amount"] = amount.issue
    amount_value = amount.value
    if amount_value is None:
        money_in = parse_amount(_cell(cells, mapping, "money_in")).value
        money_out = parse_amount(_cell(cells, mapping, "money_out")).value
        if money_in and money_out:
            parse_errors["amount"] = "Row has both money in and money out"
        elif money_in:
            amount_value, direction = money_in, TransactionKind.SALE
        elif money_out:
            amount_value, direction = money_out, TransactionKind.EXPENSE
    if amount_value is None and q.value is not None and up.value is not None:
        amount_value = (q.value * up.value).quantize(Decimal("0.01"))
        conf["amount"] = 0.95  # computed, not stated
    parsed["amount"] = str(amount_value) if amount_value is not None else None
    conf.setdefault("amount", 1.0 if amount_value is not None else 0.0)

    # --- kind: explicit column > description words > category > in/out column > batch default
    describing = " ".join(
        filter(None, [parsed["description"], parsed["product"], text("category")])
    )
    kind_guess = guess_kind(text("kind"))
    if kind_guess.value is None and text("kind") is None:
        kind_guess = guess_kind(describing)
    if kind_guess.value is not None:
        parsed["kind"], conf["kind"] = str(kind_guess.value), kind_guess.confidence
        if text("kind") is not None:
            conf["kind"] = max(conf["kind"], 0.9)  # the user labelled it themselves
    elif text("kind") is not None:
        parsed["kind"], conf["kind"] = None, 0.0
        parse_errors["kind"] = f"Don't know what transaction type '{text('kind')}' is"
    elif direction is not None:
        # Money out with no other clue: an expense is likely, but could be a purchase/personal.
        parsed["kind"] = str(direction)
        conf["kind"] = 0.9 if direction == TransactionKind.SALE else 0.6
        if direction == TransactionKind.EXPENSE and guess_category(describing).value:
            conf["kind"] = 0.85
    elif default_kind is not None:
        parsed["kind"], conf["kind"] = str(default_kind), 0.9
    elif guess_category(describing).value:
        parsed["kind"], conf["kind"] = str(TransactionKind.EXPENSE), 0.7
    else:
        parsed["kind"], conf["kind"] = None, 0.0

    # Customer/supplier columns are strong hints when the kind is still unknown.
    if parsed["kind"] is None and parsed["customer"]:
        parsed["kind"], conf["kind"] = str(TransactionKind.SALE), 0.6
    elif parsed["kind"] is None and parsed["supplier"]:
        parsed["kind"], conf["kind"] = str(TransactionKind.PURCHASE), 0.6

    # --- category (only meaningful for expenses)
    raw_category = text("category")
    if raw_category:
        parsed["category"], conf["category"] = normalize_category(raw_category), 0.95
    else:
        g = guess_category(parsed["description"] or parsed["product"])
        parsed["category"], conf["category"] = g.value, g.confidence

    # --- settlement
    raw_settlement = normalize_key(text("settlement") or "")
    if raw_settlement and any(w in raw_settlement for w in _CREDIT_WORDS):
        parsed["settlement"] = str(Settlement.CREDIT)
    else:
        parsed["settlement"] = str(Settlement.PAID)

    parsed["field_confidence"] = conf
    parsed["parse_errors"] = parse_errors
    return parsed


def validate_parsed(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    errors: dict[str, str] = parsed.get("parse_errors", {})
    conf: dict[str, float] = parsed.get("field_confidence", {})

    for field, message in errors.items():
        issues.append(_issue(field, message, blocking=field in ("occurred_on", "amount", "kind")))

    if not parsed.get("occurred_on") and "occurred_on" not in errors:
        issues.append(_issue("occurred_on", "Date is missing", blocking=True))
    if not parsed.get("amount") and "amount" not in errors:
        issues.append(_issue("amount", "Amount is missing", blocking=True))
    elif parsed.get("amount") and Decimal(parsed["amount"]) <= 0:
        issues.append(_issue("amount", "Amount must be greater than zero", blocking=True))

    kind = parsed.get("kind")
    if not kind:
        if "kind" not in errors:
            issues.append(
                _issue("kind", "Is this a sale, a purchase, an expense or personal?", blocking=True)
            )
    elif conf.get("kind", 1.0) < REVIEW_THRESHOLD:
        issues.append(
            _issue("kind", f"Please confirm this is a {kind.replace('_', ' ')}", blocking=False)
        )

    if kind == TransactionKind.EXPENSE and not parsed.get("category"):
        issues.append(_issue("category", "Expense category is unknown", blocking=False))

    description = normalize_key(
        " ".join(filter(None, [parsed.get("description"), parsed.get("product")]))
    )
    if description.startswith("total") or description in ("grand total", "sum", "subtotal"):
        issues.append(_issue("description", "This looks like a totals row", blocking=True))

    return issues


def row_confidence(parsed: dict[str, Any], issues: list[dict[str, Any]]) -> float:
    if any(i["blocking"] for i in issues):
        return 0.0
    conf = parsed.get("field_confidence", {})
    core = [conf.get("amount", 0.0), conf.get("kind", 0.0)]
    return round(min(core), 3)
