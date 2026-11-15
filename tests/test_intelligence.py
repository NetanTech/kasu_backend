"""Scenario: a provisions shop, September vs October 2026.

* Rice supplier raises cost 160,000 -> 193,600 per bag (+21%); shop raises price only 8%.
* Rice sales fall from 8 bags to 4 -> revenue drops ~27%.
* Beans are new in October and selling fast -> low stock.
* Groundnut oil bought in July, never sold -> dead stock.
* Owner withdraws more than the month's profit.
* Chidi used to buy every two weeks and has gone quiet -> dormant.
"""

from decimal import Decimal

import pytest

OCT = {"start": "2026-10-01", "end": "2026-10-31"}


def _post(api, kind, amount, day, **extra):
    res = api.post(
        "/transactions",
        json={"kind": kind, "amount": str(amount), "occurred_on": day, **extra},
    )
    assert res.status_code == 201, res.text


def _rice(api, kind, amount, day, qty="1", **extra):
    _post(api, kind, amount, day, product_name="Rice", quantity=qty, unit="bag", **extra)


@pytest.fixture
def shop(api):
    # Dead stock: bought in July, never sold.
    _post(api, "purchase", 90_000, "2026-07-01", product_name="Groundnut oil", quantity="3")
    # Chidi: regular every ~2 weeks, then stops (sales without product).
    for day in ("2026-07-01", "2026-07-15", "2026-08-01", "2026-08-15"):
        _post(api, "sale", 40_000, day, counterparty_name="Chidi")

    # September: 10 bags @160k, sell 8 @200k.
    _rice(api, "purchase", 1_600_000, "2026-09-01", qty="10")
    for n, day in enumerate(["03", "06", "09", "12", "15", "18", "21", "24"]):
        _rice(
            api,
            "sale",
            200_000,
            f"2026-09-{day}",
            counterparty_name="Mrs Bello" if n % 2 == 0 else "Ade",
        )
    _post(api, "expense", 20_000, "2026-09-10", category="transport")

    # October: 10 bags @193.6k (+21%), sell only 4 @216k (+8%).
    _rice(api, "purchase", 1_936_000, "2026-10-01", qty="10")
    for day in ("05", "12", "19", "26"):
        _rice(api, "sale", 216_000, f"2026-10-{day}", counterparty_name="Mrs Bello")
    # Beans: 6 bought, 5 sold -> about 6 days of stock left.
    _post(api, "purchase", 300_000, "2026-10-02", product_name="Beans", quantity="6")
    for i, day in enumerate(("10", "14", "18", "22", "28")):
        _post(
            api,
            "sale",
            60_000,
            f"2026-10-{day}",
            product_name="Beans",
            quantity="1",
            counterparty_name="Kemi",
            settlement="credit" if i == 0 else "paid",
        )
    _post(api, "expense", 30_000, "2026-10-15", category="transport")
    _post(api, "owner_withdrawal", 300_000, "2026-10-20", description="school fees")
    return api


def _codes(insights):
    return [i["code"] for i in insights]


def test_insights_explain_what_happened_and_why(shop):
    insights = shop.get("/insights", params=OCT).json()["insights"]
    by_code = {i["code"]: i for i in insights}

    # Sorted most severe first.
    order = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
    assert [order[i["severity"]] for i in insights] == sorted(
        order[i["severity"]] for i in insights
    )

    drop = by_code["revenue_down"]
    assert drop["severity"] == "critical"  # -27%
    assert drop["evidence"]["change_pct"] == "-27.3"
    assert "Rice" in drop["why"]

    cost = by_code["input_cost_up"]
    assert cost["evidence"]["cost_change_pct"] == "21.0"
    assert cost["evidence"]["selling_price_change_pct"] == "8.0"
    assert "earns less" in cost["why"]

    margin = by_code["margin_down"]
    assert margin["evidence"] == {"gross_margin_pct": "13.9", "previous_gross_margin_pct": "20.0"}
    assert "Rice +21.0%" in margin["why"]

    assert by_code["expense_category_up"]["evidence"]["category"] == "transport"
    assert by_code["withdrawals_exceed_profit"]["evidence"] == {
        "net_profit": "132000.00",
        "withdrawn": "300000.00",
    }
    squeeze = by_code["cash_squeeze"]
    assert "stock bought but not yet sold" in squeeze["why"]

    low = by_code["low_stock"]
    assert "Beans" in low["title"]
    assert low["evidence"]["reorder"]["suggested_quantity"] == "2"

    assert "Groundnut oil" in by_code["dead_stock"]["what_happened"]
    assert by_code["customer_concentration"]["evidence"]["customer"] == "Mrs Bello"
    assert "Chidi" in by_code["dormant_customers"]["what_happened"]


def test_insights_cost_impact_is_correct(shop):
    cost = next(
        i
        for i in shop.get("/insights", params=OCT).json()["insights"]
        if i["code"] == "input_cost_up"
    )
    # (193,600 - 160,000) x 10 bags bought in October
    assert cost["potential_impact"].startswith("About ₦336,000")


def test_inventory(shop):
    inv = shop.get("/analytics/inventory", params={"as_of": "2026-10-31"}).json()
    by_name = {p["name"]: p for p in inv["products"]}

    beans = by_name["Beans"]
    assert beans["status"] == "low"
    assert beans["estimated_on_hand"] == "1.0000"
    assert beans["estimated_days_left"] == "6.0"
    assert beans["reorder"]["suggested_quantity"] == "2"
    assert beans["reorder"]["estimated_cost"] == "100000.00"

    rice = by_name["Rice"]
    assert rice["status"] == "ok"
    assert rice["movement"] == "fast"
    assert rice["estimated_on_hand"] == "8.0000"
    assert rice["average_cost"] == "188000.00"  # WAC: (2 x 160k + 10 x 193.6k) / 12

    assert by_name["Groundnut oil"]["status"] == "dead"
    assert inv["summary"]["dead_stock_value"] == "90000.00"
    assert inv["summary"]["basis"] == "estimated"
    assert "_items" not in inv  # internal data never leaks into the API


def test_customers(shop):
    c = shop.get("/analytics/customers", params=OCT).json()
    s = c["summary"]
    assert (s["active_customers"], s["new_customers"], s["returning_customers"]) == (2, 1, 1)
    assert s["top_customer_share_pct"] == "74.2"
    assert c["top_customers"][0]["name"] == "Mrs Bello"
    assert [d["name"] for d in c["dormant_customers"]] == ["Chidi"]  # Ade bought recently
    assert c["customers_owing"] == [
        {"customer_id": c["customers_owing"][0]["customer_id"], "name": "Kemi", "owing": "60000.00"}
    ]
    assert c["confidence"]["level"] == "high"


def test_health_score(shop):
    h = shop.get("/health-score", params=OCT).json()
    dims = {d["key"]: d for d in h["dimensions"]}
    assert sum(d["weight"] for d in h["dimensions"]) == 100
    assert dims["inflation_resilience"]["score"] is None  # not faked
    assert h["coverage_pct"] == 95
    assert 0 <= h["score"] <= 100
    assert h["band"] in ("good", "fair", "needs_attention")
    assert dims["revenue"]["score"] == 0  # -27% is below the -20% floor
    assert h["confidence"]["level"] == "high"
    assert "not a definitive" in h["note"]


def test_weekly_brief(shop):
    # The scenario's last week has only two sales; a brief needs at least three records.
    _post(shop, "expense", 10_000, "2026-10-30", category="transport")
    b = shop.get("/brief", params={"week_ending": "2026-10-31"}).json()
    assert b["status"] == "ready"
    assert b["period"] == {"start": "2026-10-25", "end": "2026-10-31"}
    lines = b["highlights"]
    assert lines[0]["text"].startswith("Sales were ₦276,000")
    assert lines[0]["metric"] == "revenue" and lines[0]["evidence"]["revenue"] == "276000.00"
    stock = next(line for line in lines if "fastest-selling" in line["text"])
    assert "Rice" in stock["text"] or "Beans" in stock["text"]
    # Stock from records alone is an estimate, never reported as high confidence.
    assert stock["basis"] == "estimated" and stock["confidence"] != "high"
    assert b["text"].startswith("*Mama Nkechi Provisions: your week (25 Oct – 31 Oct)*")
    assert "Business Health Score" in b["text"]
    # At least one recommendation, each with the evidence and confidence behind it.
    assert 1 <= len(b["recommendations"]) <= 3
    for r in b["recommendations"]:
        assert r["text"] and r["because"] and r["evidence"]
        assert r["basis"] == "recommended" and r["confidence"] in ("high", "medium", "low")


def test_dashboard(shop):
    d = shop.get("/dashboard", params=OCT).json()
    assert d["headline"]["revenue"]["value"] == "1164000.00"
    assert d["business_vs_personal"]["retained_in_business"]["value"] == "-168000.00"
    assert d["alerts"][0]["code"] == "revenue_down"
    assert len(d["alerts"]) <= 5
    assert d["top_products"][0]["name"] == "Rice"
    assert d["inventory"]["by_status"]["low"] == 1
    assert [t["period_start"] for t in d["trend"]][-1] == "2026-10-01"
    assert d["inflation"]["available"] is False
    assert Decimal(d["health"]["score"]) >= 0


def test_empty_business_doesnt_crash(api):
    d = api.get("/dashboard", params=OCT).json()
    assert d["health"]["score"] is None
    assert d["alerts"] == []
    assert d["confidence"]["level"] == "low"
    b = api.get("/brief", params={"week_ending": "2026-10-31"}).json()
    assert b["status"] == "insufficient_data"
    assert b["prompt"].startswith("No transactions were recorded this week.")
    assert api.get("/insights", params=OCT).json()["insights"] == []
