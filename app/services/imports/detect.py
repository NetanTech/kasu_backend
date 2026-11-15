"""Find the header row and map spreadsheet columns to canonical fields (PRD §7.1)."""

from typing import Any

from app.services.normalization import normalize_key

FIELDS = (
    "date",
    "kind",
    "description",
    "product",
    "quantity",
    "unit_price",
    "amount",
    "money_in",
    "money_out",
    "category",
    "customer",
    "supplier",
    "settlement",
)

# Normalized header text -> field. Order within each list does not matter.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("date", "day", "txn date", "transaction date", "value date", "posting date", "when"),
    "kind": ("type", "transaction type", "txn type", "kind", "entry type", "sales or expense"),
    "description": (
        "description",
        "details",
        "narration",
        "remark",
        "remarks",
        "note",
        "notes",
        "particulars",
        "memo",
        "purpose",
    ),
    "product": ("product", "item", "items", "goods", "product name", "item name", "stock"),
    "quantity": ("qty", "quantity", "units", "no of items", "pcs", "pieces", "number"),
    "unit_price": ("price", "unit price", "rate", "price per unit", "unit cost", "selling price"),
    "amount": ("amount", "total", "value", "amount ngn", "total amount", "naira", "cost", "sum"),
    "money_in": ("money in", "inflow", "credit", "cr", "deposit", "income", "received", "sales"),
    "money_out": (
        "money out",
        "outflow",
        "debit",
        "dr",
        "withdrawal",
        "expense",
        "expenses",
        "paid",
        "spent",
        "payment",
    ),
    "category": ("category", "expense type", "expense category", "class", "account"),
    "customer": ("customer", "client", "buyer", "customer name", "sold to"),
    "supplier": ("supplier", "vendor", "seller", "supplier name", "bought from"),
    "settlement": ("payment status", "status", "paid or credit", "payment type", "mode"),
}

_LOOKUP = {normalize_key(s): f for f, words in SYNONYMS.items() for s in words}


def field_for_header(header: Any) -> str | None:
    if header is None:
        return None
    key = normalize_key(str(header))
    if not key:
        return None
    if key in _LOOKUP:
        return _LOOKUP[key]
    # "Amount (₦)" / "Total Amount Paid" -> amount; prefer the longest synonym contained.
    best = None
    for synonym, f in _LOOKUP.items():
        if (
            len(synonym) >= 3
            and f" {synonym} " in f" {key} "
            and (best is None or len(synonym) > len(best[0]))
        ):
            best = (synonym, f)
    return best[1] if best else None


def detect_header_row(rows: list[list[Any]], scan: int = 15) -> int | None:
    """The header is the row (among the first few) whose cells look most like known headers."""
    best_index, best_score = None, 1
    for i, row in enumerate(rows[:scan]):
        score = len({f for f in (field_for_header(c) for c in row) if f})
        if score > best_score:
            best_index, best_score = i, score
    return best_index


def map_columns(headers: list[Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        f = field_for_header(header)
        if f and f not in mapping:
            mapping[f] = index
    # A sheet with explicit in/out columns shouldn't also treat "Total" as the amount.
    if "money_in" in mapping and "money_out" in mapping:
        mapping.pop("amount", None)
    return mapping
