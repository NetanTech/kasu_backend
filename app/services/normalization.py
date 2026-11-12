"""Turning messy human input into canonical values (PRD §9).

Rule of thumb: when something can't be resolved reliably, return an *issue* rather than guess.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from dateutil import parser as date_parser

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

_CURRENCY_MARKERS = re.compile(r"(₦|ngn|naira|n(?=\d))", re.IGNORECASE)
_MULTIPLIERS = {"k": Decimal(1_000), "m": Decimal(1_000_000)}

# Excel stores dates as days since 1899-12-30 (with the 1900 leap-year bug baked in).
_EXCEL_EPOCH = date(1899, 12, 30)


def normalize_key(text: str) -> str:
    """Comparison key for names: lowercase, no punctuation, single spaces."""
    text = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_key(a), normalize_key(b)).ratio()


@dataclass(frozen=True)
class Parsed[T]:
    value: T | None
    issue: str | None = None


def to_minor(amount: Decimal) -> int:
    """₦ 1,234.56 -> 123456 kobo."""
    return int((amount * 100).quantize(Decimal(1)))


def from_minor(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


def parse_amount(raw: object) -> Parsed[Decimal]:
    """Parse amounts like 5000, "₦5,000", "5k", "(2,500)", "NGN 1.2m".

    Returns the absolute value; sign is carried by the transaction kind, not the amount.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return Parsed(None)
    if isinstance(raw, bool):
        return Parsed(None, f"'{raw}' is not an amount")
    if isinstance(raw, int | float | Decimal):
        try:
            value = abs(Decimal(str(raw)))
        except InvalidOperation:
            return Parsed(None, f"'{raw}' is not an amount")
        return Parsed(value.quantize(Decimal("0.01")))

    text = str(raw).strip().lower()
    text = _CURRENCY_MARKERS.sub("", text)
    text = text.replace(",", "").replace(" ", "").strip("()-+")
    multiplier = Decimal(1)
    if text and text[-1] in _MULTIPLIERS:
        multiplier = _MULTIPLIERS[text[-1]]
        text = text[:-1]
    try:
        value = Decimal(text) * multiplier
    except InvalidOperation:
        return Parsed(None, f"Could not read '{raw}' as an amount")
    return Parsed(abs(value).quantize(Decimal("0.01")))


def parse_quantity(raw: object) -> Parsed[Decimal]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return Parsed(None)
    if isinstance(raw, int | float | Decimal) and not isinstance(raw, bool):
        return Parsed(Decimal(str(raw)))
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(raw))
    if not match:
        return Parsed(None, f"Could not read '{raw}' as a quantity")
    return Parsed(Decimal(match.group(1)))


def parse_date(raw: object) -> Parsed[date]:
    """Parse dates, assuming day-first (Nigerian convention: 03/04/2026 = 3 April)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return Parsed(None)
    if isinstance(raw, datetime):
        return Parsed(raw.date())
    if isinstance(raw, date):
        return Parsed(raw)
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        # Excel serial date (plausible range: 1990..2100)
        if 32874 <= raw <= 73051:
            return Parsed(_EXCEL_EPOCH + timedelta(days=int(raw)))
        return Parsed(None, f"'{raw}' is not a date")
    text = str(raw).strip()
    try:
        # ISO (YYYY-MM-DD) is unambiguous; dayfirst would wrongly swap its month and day.
        return Parsed(date.fromisoformat(text[:10]))
    except ValueError:
        pass
    try:
        return Parsed(date_parser.parse(text, dayfirst=True).date())
    except (ValueError, OverflowError):
        return Parsed(None, f"Could not read '{raw}' as a date")
