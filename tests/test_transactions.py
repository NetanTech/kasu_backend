import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def _sale(**overrides):
    return {"kind": "sale", "occurred_on": "2026-10-05", "amount": "25000", **overrides}


def test_create_and_list(api):
    res = api.post(
        "/transactions",
        json=_sale(product_name="Rice", quantity="1", counterparty_name="Mrs Bello"),
    )
    assert res.status_code == 201, res.text
    txn = res.json()
    assert txn["amount_minor"] == 2_500_000
    assert txn["amount"] == "25000.00"
    assert txn["product_name"] == "Rice"
    assert txn["counterparty_name"] == "Mrs Bello"
    assert txn["status"] == "confirmed"

    page = api.get("/transactions").json()
    assert page["total"] == 1
    assert api.get("/transactions", params={"q": "bello"}).json()["total"] == 1
    assert api.get("/transactions", params={"kind": "expense"}).json()["total"] == 0


def test_validation(api):
    assert api.post("/transactions", json=_sale(amount="0")).status_code == 422
    assert api.post("/transactions", json=_sale(amount="10.555")).status_code == 422
    assert api.post("/transactions", json=_sale(occurred_on="2099-01-01")).status_code == 422


def test_client_ref_is_idempotent(api):
    first = api.post("/transactions", json=_sale(client_ref="phone-1:abc"))
    again = api.post("/transactions", json=_sale(client_ref="phone-1:abc"))
    assert first.status_code == 201
    assert again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert api.get("/transactions").json()["total"] == 1


def test_product_aliases_resolve_to_one_product(api):
    a = api.post("/transactions", json=_sale(product_name="Rice", quantity="1")).json()
    b = api.post("/transactions", json=_sale(product_name="  RICE ", quantity="1")).json()
    assert a["product_id"] == b["product_id"]


def test_ambiguous_product_asks_instead_of_guessing(api):
    api.post("/transactions", json=_sale(product_name="Golden Penny Semovita", quantity="1"))
    res = api.post("/transactions", json=_sale(product_name="Golden Peny Semovita", quantity="1"))
    assert res.status_code == 409
    assert res.json()["detail"]["suggestion"] == "Golden Penny Semovita"

    forced = api.post(
        "/transactions",
        json=_sale(product_name="Golden Peny Semovita", quantity="1", product_is_new=True),
    )
    assert forced.status_code == 201


def test_correction_keeps_history(api):
    txn = api.post("/transactions", json=_sale()).json()
    res = api.patch(
        f"/transactions/{txn['id']}", json={"amount": "27000", "reason": "Mistyped amount"}
    )
    assert res.status_code == 200
    assert res.json()["amount"] == "27000.00"
    assert res.json()["version"] == 2

    history = api.get(f"/transactions/{txn['id']}/history").json()
    assert [e["event_type"] for e in history] == ["created", "corrected"]
    assert history[0]["snapshot"]["amount_minor"] == 2_500_000  # original preserved
    assert history[1]["changes"] == {"amount_minor": [2_500_000, 2_700_000]}
    assert history[1]["reason"] == "Mistyped amount"


def test_void_hides_from_list_and_analytics(api):
    txn = api.post("/transactions", json=_sale()).json()
    assert (
        api.post(f"/transactions/{txn['id']}/void", json={"reason": "entered twice"}).status_code
        == 200
    )
    assert api.get("/transactions").json()["total"] == 0
    assert api.get("/transactions", params={"status": "voided"}).json()["total"] == 1

    overview = api.get("/analytics/overview", params={"start": "2026-10-01", "end": "2026-10-31"})
    assert overview.json()["revenue"]["value"] == "0.00"
    # Voided transactions can't be corrected or re-voided
    assert api.patch(f"/transactions/{txn['id']}", json={"amount": "1"}).status_code == 422
    assert api.post(f"/transactions/{txn['id']}/void", json={}).status_code == 409


def test_events_table_is_append_only(api, db):
    api.post("/transactions", json=_sale())
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE transaction_events SET reason = 'tampered'"))
    db.rollback()
