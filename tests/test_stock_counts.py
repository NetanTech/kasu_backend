from tests.conftest import register

OCT = {"start": "2026-10-01", "end": "2026-10-31"}


def _post(api, kind, amount, day, **extra):
    res = api.post(
        "/transactions", json={"kind": kind, "amount": str(amount), "occurred_on": day, **extra}
    )
    assert res.status_code == 201, res.text
    return res.json()


def _count(api, day, *items, expect=201):
    res = api.post("/stock-counts", json={"counted_on": day, "items": list(items)})
    assert res.status_code == expect, res.text
    return res.json()


def _stock(api, name, as_of="2026-10-31"):
    inv = api.get("/analytics/inventory", params={"as_of": as_of}).json()
    return next(p for p in inv["products"] if p["name"] == name)


def _rice_shop(api):
    # 10 bags @10,000; 6 sold @15,000 -> records say 4 left.
    _post(api, "purchase", 100_000, "2026-10-01", product_name="Rice", quantity="10")
    for day in ("02", "03", "04", "05", "06", "07"):
        _post(api, "sale", 15_000, f"2026-10-{day}", product_name="Rice", quantity="1")


def test_count_reveals_missing_stock_and_reduces_profit(api):
    _rice_shop(api)
    [count] = _count(api, "2026-10-10", {"product_name": "rice", "counted_qty": "3"})
    assert count["product_name"] == "Rice"  # resolved via alias, not a new product
    assert count["expected_qty"] == "4.0000"
    assert count["variance_qty"] == "-1.0000"
    assert count["variance_value"] == "-10000.00"

    o = api.get("/analytics/overview", params=OCT).json()
    assert o["gross_profit"]["value"] == "30000.00"
    assert o["stock_loss"]["value"] == "10000.00"
    assert o["net_profit"]["value"] == "20000.00"  # 30,000 gross - 10,000 missing stock

    rice = _stock(api, "Rice")
    assert rice["estimated_on_hand"] == "3.0000"
    assert rice["last_counted_on"] == "2026-10-10"
    assert rice["stock_basis"] == "counted"

    loss = next(
        i for i in api.get("/insights", params=OCT).json()["insights"] if i["code"] == "stock_loss"
    )
    assert loss["title"] == "₦10,000 of stock is unaccounted for"
    assert "Rice (1 missing)" in loss["what_happened"]
    assert loss["potential_impact"] == "This reduced your profit by ₦10,000."


def test_count_applies_at_end_of_day(api):
    _post(api, "purchase", 10_000, "2026-10-01", product_name="Beans", quantity="10")
    _post(api, "sale", 3_000, "2026-10-05", product_name="Beans", quantity="2")
    [count] = _count(api, "2026-10-05", {"product_name": "Beans", "counted_qty": "8"})
    assert count["variance_qty"] == "0.0000"  # the same-day sale happened before the count
    _post(api, "sale", 1_500, "2026-10-06", product_name="Beans", quantity="1")
    assert _stock(api, "Beans")["estimated_on_hand"] == "7.0000"


def test_count_fixes_oversold_estimate(api):
    _post(api, "purchase", 1_000, "2026-10-01", product_name="Soap", quantity="2")
    _post(api, "sale", 3_000, "2026-10-02", product_name="Soap", quantity="5")  # more than bought
    before = api.get("/analytics/inventory", params={"as_of": "2026-10-31"}).json()
    assert before["confidence"]["level"] == "medium"
    assert _stock(api, "Soap")["sold_beyond_recorded_stock"] == "3.0000"

    _count(api, "2026-10-03", {"product_name": "Soap", "counted_qty": "4"})
    after = api.get("/analytics/inventory", params={"as_of": "2026-10-31"}).json()
    soap = next(p for p in after["products"] if p["name"] == "Soap")
    assert soap["sold_beyond_recorded_stock"] == "0"
    assert soap["estimated_on_hand"] == "4.0000"
    assert after["confidence"]["level"] == "high"


def test_void_restores_estimate(api):
    _rice_shop(api)
    [count] = _count(api, "2026-10-10", {"product_name": "Rice", "counted_qty": "3"})

    res = api.post(f"/stock-counts/{count['id']}/void", json={"reason": "miscounted"})
    assert res.status_code == 200
    assert res.json()["void_reason"] == "miscounted"
    assert api.post(f"/stock-counts/{count['id']}/void", json={}).status_code == 409

    assert _stock(api, "Rice")["estimated_on_hand"] == "4.0000"
    o = api.get("/analytics/overview", params=OCT).json()
    assert o["stock_loss"]["value"] == "0.00"
    assert o["net_profit"]["value"] == "30000.00"

    assert api.get("/stock-counts").json() == []
    assert len(api.get("/stock-counts", params={"include_voided": True}).json()) == 1


def test_counting_a_product_with_no_cost_history(api):
    # Opening stock: counted before any purchase was recorded.
    [count] = _count(
        api, "2026-10-01", {"product_name": "Milo", "product_is_new": True, "counted_qty": "5"}
    )
    assert count["variance_value"] is None  # no cost to value it at
    _post(api, "sale", 2_000, "2026-10-02", product_name="Milo", quantity="2")
    assert _stock(api, "Milo")["status"] != "unknown"

    # First purchase: existing counted units are valued at this purchase's unit cost.
    _post(api, "purchase", 5_000, "2026-10-03", product_name="Milo", quantity="10")
    milo = _stock(api, "Milo")
    assert milo["estimated_on_hand"] == "13.0000"
    assert milo["average_cost"] == "500.00"


def test_surplus_is_flagged_not_counted_as_profit(api):
    _post(api, "purchase", 500, "2026-10-01", product_name="Garri", quantity="5")
    _post(api, "sale", 400, "2026-10-02", product_name="Garri", quantity="2")
    [count] = _count(api, "2026-10-05", {"product_name": "Garri", "counted_qty": "6"})
    assert count["variance_qty"] == "3.0000"

    o = api.get("/analytics/overview", params=OCT).json()
    assert o["net_profit"]["value"] == "200.00"  # 400 - 2 x 100; surplus not booked
    assert any("more stock than your records" in r for r in o["confidence"]["reasons"])
    codes = [i["code"] for i in api.get("/insights", params=OCT).json()["insights"]]
    assert "stock_surplus" in codes


def test_loss_not_double_counted_when_cogs_is_estimated_from_purchases(api):
    _post(api, "sale", 100_000, "2026-10-05")  # no product/qty -> COGS falls back to purchases
    _post(api, "purchase", 60_000, "2026-10-01", product_name="Rice", quantity="10")
    _count(api, "2026-10-10", {"product_name": "Rice", "counted_qty": "8"})

    o = api.get("/analytics/overview", params=OCT).json()
    assert o["cost_of_goods_sold"]["method"] == "purchases_in_period"
    assert o["stock_loss"]["value"] == "0.00"
    assert o["net_profit"]["value"] == "40000.00"


def test_validation(api, client):
    _post(api, "purchase", 1_000, "2026-10-01", product_name="Golden Penny Semovita", quantity="1")

    _count(
        api, "2099-01-01", {"product_name": "Golden Penny Semovita", "counted_qty": "1"}, expect=422
    )
    _count(
        api,
        "2026-10-02",
        {"product_name": "Golden Penny Semovita", "counted_qty": "-1"},
        expect=422,
    )
    _count(
        api,
        "2026-10-02",
        {"product_name": "Golden Penny Semovita", "counted_qty": "1"},
        {"product_name": "golden penny semovita", "counted_qty": "2"},
        expect=422,
    )
    ambiguous = _count(
        api, "2026-10-02", {"product_name": "Golden Peny Semovita", "counted_qty": "1"}, expect=409
    )
    assert ambiguous["detail"]["suggestion"] == "Golden Penny Semovita"

    # Another tenant can't count this business's product.
    product_id = api.get("/products").json()[0]["id"]
    other = register(client)
    other_biz = client.post("/v1/businesses", json={"name": "Other"}, headers=other).json()["id"]
    res = client.post(
        f"/v1/businesses/{other_biz}/stock-counts",
        headers=other,
        json={"counted_on": "2026-10-02", "items": [{"product_id": product_id, "counted_qty": 1}]},
    )
    assert res.status_code == 422
    assert (
        client.get(
            f"/v1/businesses/{api.base.split('/')[-1]}/stock-counts", headers=other
        ).status_code
        == 404
    )
