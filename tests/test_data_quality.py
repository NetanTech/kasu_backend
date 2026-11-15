"""KASU-018: extraction accuracy on Nigerian-style records, and live data-quality tracking."""

from decimal import Decimal

import pytest

from app.evaluation.accuracy import CRITICAL_ACCURACY_TARGET, PRECISION_TARGET, evaluate
from app.services.imports.rows import REVIEW_THRESHOLD, extract_row
from app.services.normalization import parse_amount, parse_date, parse_quantity
from tests.test_imports import CSV, _upload


def test_accuracy_targets_are_met():
    report = evaluate()
    summary = report.as_dict()
    assert report.critical_accuracy_pct >= CRITICAL_ACCURACY_TARGET, summary
    assert report.precision_pct >= PRECISION_TARGET, summary
    # A wrong number with no warning is the worst outcome; there must be none.
    assert all(f["silent_errors"] == 0 for f in summary["critical_fields"]["by_field"].values())
    assert report.cases >= 60


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("#25,000", "25000.00"), ("₦5,000/-", "5000.00"), ("N2.5m", "2500000.00")],
)
def test_amount_spellings(raw, expected):
    assert parse_amount(raw).value == Decimal(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1/2 bag", "0.5"),
        ("½", "0.5"),
        ("1 1/2 bags", "1.5"),
        ("1½ jerrycan", "1.5"),
        ("20/2", "10"),
        ("2.5kg", "2.5"),
    ],
)
def test_fractional_quantities(raw, expected):
    assert parse_quantity(raw).value == Decimal(expected)
    assert parse_quantity("1/0").value is None


def test_year_first_dates_are_not_day_first():
    assert parse_date("2026/11/04").value.isoformat() == "2026-11-04"
    assert parse_date("2026-11-04 14:32:00").value.isoformat() == "2026-11-04"
    assert parse_date("04/11/2026").value.isoformat() == "2026-11-04"
    assert parse_date("2026/13/01").value is None


def _kind(row: dict) -> tuple[str | None, float]:
    names = list(row)
    parsed = extract_row([row[n] for n in names], {n: i for i, n in enumerate(names)})
    return parsed["kind"], parsed["field_confidence"]["kind"]


def test_money_direction_beats_misleading_words():
    kind, conf = _kind({"description": "Wages for sales girl", "money_out": "25,000"})
    assert kind == "expense" and conf >= REVIEW_THRESHOLD


def test_mixed_signals_are_sent_for_review():
    kind, conf = _kind({"description": "Keke to deliver goods", "money_out": "1,500"})
    assert conf < REVIEW_THRESHOLD


def test_data_quality_tracks_unresolved_and_corrected_records(api):
    batch = _upload(api, CSV).json()
    committed = api.post(f"/imports/{batch['id']}/commit")
    assert committed.status_code == 200, committed.text

    sale = api.post(
        "/transactions", json={"kind": "sale", "amount": "5000", "occurred_on": "2026-10-07"}
    ).json()
    other = api.post(
        "/transactions", json={"kind": "sale", "amount": "7000", "occurred_on": "2026-10-08"}
    ).json()
    fixed = api.patch(
        f"/transactions/{sale['id']}", json={"amount": "50000", "reason": "missed a zero"}
    )
    assert fixed.status_code == 200, fixed.text
    # A description-only correction isn't a critical-field error.
    api.patch(f"/transactions/{other['id']}", json={"description": "evening sales"})

    q = api.get("/analytics/data-quality").json()
    unresolved = q["unresolved_records"]
    assert unresolved["import_rows"] == 6
    assert unresolved["unresolved"] == 2  # the "abc" amount and the totals row
    assert unresolved["unresolved_rate_pct"] == "33.3"
    assert unresolved["provisional"] == 1  # "keke to market" awaits confirmation

    errors = q["critical_field_errors"]
    assert errors["transactions"] == 6  # 4 imported + 2 manual
    assert errors["corrected"] == 1
    assert errors["by_field"]["amount_minor"] == 1
    assert errors["by_source"]["manual"] == {
        "transactions": 2,
        "corrected": 1,
        "error_rate_pct": "50.0",
    }
    assert errors["by_source"]["import"]["error_rate_pct"] == "0.0"
