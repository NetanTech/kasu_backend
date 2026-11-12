from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models.enums import TransactionKind
from app.services.classification import guess_category, guess_kind
from app.services.normalization import normalize_key, parse_amount, parse_date, to_minor


@pytest.mark.parametrize(
    "raw, expected",
    [
        (5000, "5000.00"),
        ("5000", "5000.00"),
        ("₦5,000", "5000.00"),
        ("N5,000.50", "5000.50"),
        ("NGN 12,500", "12500.00"),
        ("5k", "5000.00"),
        ("1.2m", "1200000.00"),
        ("(2,500)", "2500.00"),
        ("-300", "300.00"),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw).value == Decimal(expected)


def test_parse_amount_rejects_garbage():
    result = parse_amount("five thousand")
    assert result.value is None and result.issue


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("03/04/2026", date(2026, 4, 3)),  # day-first
        ("2026-04-03", date(2026, 4, 3)),
        ("3 Apr 2026", date(2026, 4, 3)),
        (datetime(2026, 4, 3, 10, 0), date(2026, 4, 3)),
        (46115, date(2026, 4, 3)),  # Excel serial
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw).value == expected


def test_minor_units_round_trip():
    assert to_minor(Decimal("1234.56")) == 123456


def test_normalize_key():
    assert normalize_key("  RICE (50kg)!! ") == "rice 50kg"


def test_classification():
    assert guess_kind("Sold 2 bags").value == TransactionKind.SALE
    assert guess_kind("house rent for family").value == TransactionKind.PERSONAL_EXPENSE
    assert guess_category("Paid NEPA bill").value == "utilities"
    assert guess_category("keke to market").value == "transport"
    assert guess_kind("something unclear").value is None
