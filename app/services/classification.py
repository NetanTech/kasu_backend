"""Rule-based classification of free text into transaction kinds and expense categories.

This is the deterministic baseline. An LLM classifier will sit on top of it later; rules stay as
the fast path and as a fallback. Every guess carries a confidence so the caller can decide
whether to ask the user (PRD §7.1, §19).
"""

from dataclasses import dataclass

from app.models.enums import TransactionKind
from app.services.normalization import normalize_key

EXPENSE_CATEGORIES = (
    "rent",
    "salaries",
    "transport",
    "fuel",
    "utilities",
    "communication",
    "marketing",
    "repairs",
    "supplies",
    "bank_charges",
    "taxes_levies",
    "other",
)

_KIND_WORDS: dict[TransactionKind, tuple[str, ...]] = {
    TransactionKind.SALE: ("sale", "sales", "sold", "income", "revenue", "credit sale"),
    TransactionKind.PURCHASE: (
        "purchase",
        "purchases",
        "bought",
        "buy",
        "stock",
        "restock",
        "inventory",
        "goods",
    ),
    TransactionKind.EXPENSE: ("expense", "expenses", "spent", "spend", "cost", "paid for"),
    TransactionKind.PERSONAL_EXPENSE: (
        "personal",
        "household",
        "house",
        "home",
        "school fees",
        "family",
        "wife",
        "husband",
        "children",
        "kids",
    ),
    TransactionKind.OWNER_WITHDRAWAL: ("withdrawal", "withdraw", "drawings", "owner took"),
    TransactionKind.OWNER_CONTRIBUTION: ("capital", "contribution", "owner added", "investment"),
    TransactionKind.OTHER_INCOME: ("other income", "interest", "refund received"),
    TransactionKind.CUSTOMER_PAYMENT: ("customer paid", "payment received", "debt paid"),
    TransactionKind.SUPPLIER_PAYMENT: ("paid supplier", "supplier payment"),
}

_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "rent": ("rent", "shop rent", "lease"),
    "salaries": ("salary", "salaries", "wage", "wages", "staff pay", "apprentice"),
    "transport": ("transport", "okada", "keke", "bus", "uber", "bolt", "delivery", "logistics"),
    "fuel": ("fuel", "petrol", "diesel", "gen fuel", "generator"),
    "utilities": ("nepa", "phcn", "electricity", "light bill", "water", "prepaid meter", "power"),
    "communication": ("airtime", "data", "recharge", "internet", "wifi"),
    "marketing": ("advert", "advertising", "marketing", "promo", "flyer", "instagram ads"),
    "repairs": ("repair", "repairs", "maintenance", "fix", "servicing"),
    "supplies": ("nylon", "packaging", "supplies", "stationery", "cleaning"),
    "bank_charges": ("bank charge", "charges", "sms alert", "stamp duty", "pos charge", "fee"),
    "taxes_levies": ("tax", "levy", "levies", "lga", "association dues", "permit"),
}


@dataclass(frozen=True)
class Guess:
    value: str | None
    confidence: float


def _match(text: str, table: dict) -> list:
    key = f" {normalize_key(text)} "
    return [label for label, words in table.items() if any(f" {w} " in key for w in words)]


def guess_kind(text: str | None) -> Guess:
    if not text:
        return Guess(None, 0.0)
    hits = _match(text, _KIND_WORDS)
    # Personal markers beat generic "expense" words: "house rent expense" is personal.
    if TransactionKind.PERSONAL_EXPENSE in hits:
        return Guess(TransactionKind.PERSONAL_EXPENSE, 0.75)
    if len(hits) == 1:
        return Guess(hits[0], 0.85)
    if hits:
        return Guess(hits[0], 0.5)
    return Guess(None, 0.0)


def guess_category(text: str | None) -> Guess:
    if not text:
        return Guess(None, 0.0)
    hits = _match(text, _CATEGORY_WORDS)
    if len(hits) == 1:
        return Guess(hits[0], 0.85)
    if hits:
        return Guess(hits[0], 0.5)
    return Guess(None, 0.0)


def normalize_category(text: str | None) -> str | None:
    if not text:
        return None
    key = normalize_key(text).replace(" ", "_")
    if key in EXPENSE_CATEGORIES:
        return key
    return guess_category(text).value or key[:100] or None
