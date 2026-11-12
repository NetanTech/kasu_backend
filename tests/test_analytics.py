OCT = {"start": "2026-10-01", "end": "2026-10-31"}


def _txn(api, kind, amount, day="2026-10-10", **extra):
    res = api.post(
        "/transactions", json={"kind": kind, "amount": str(amount), "occurred_on": day, **extra}
    )
    assert res.status_code == 201, res.text
    return res.json()


def _v(overview, key):
    return overview[key]["value"]


def test_profit_vs_withdrawals(api):
    """PRD §6: an owner withdrawal is cash out, not an operating expense."""
    _txn(api, "purchase", 320_000, "2026-10-01", product_name="Rice", quantity="2")
    _txn(api, "sale", 200_000, "2026-10-03", product_name="Rice", quantity="1")
    _txn(api, "expense", 10_000, "2026-10-04", category="rent")
    _txn(api, "owner_withdrawal", 200_000, "2026-10-05", description="House rent")

    o = api.get("/analytics/overview", params=OCT).json()
    assert _v(o, "revenue") == "200000.00"
    assert _v(o, "cost_of_goods_sold") == "160000.00"
    assert o["cost_of_goods_sold"]["method"] == "weighted_average_cost"
    assert o["cost_of_goods_sold"]["basis"] == "calculated"
    assert _v(o, "gross_profit") == "40000.00"
    assert o["gross_margin_pct"] == "20.0"
    assert _v(o, "net_profit") == "30000.00"  # withdrawal did NOT reduce profit
    assert _v(o, "owner_withdrawals") == "200000.00"
    assert _v(o, "retained_in_business") == "-170000.00"
    assert _v(o, "cash_in") == "200000.00"
    assert _v(o, "cash_out") == "530000.00"
    assert _v(o, "net_cash_flow") == "-330000.00"


def test_weighted_average_cost(api):
    _txn(api, "purchase", 200, "2026-10-01", product_name="Garri", quantity="2")  # @100
    _txn(api, "purchase", 280, "2026-10-02", product_name="Garri", quantity="2")  # @140
    _txn(api, "sale", 600, "2026-10-03", product_name="Garri", quantity="3")  # avg 120

    o = api.get("/analytics/overview", params=OCT).json()
    assert _v(o, "cost_of_goods_sold") == "360.00"
    assert _v(o, "gross_profit") == "240.00"

    p = api.get("/analytics/products", params=OCT).json()["products"]
    assert p[0]["name"] == "Garri"
    assert p[0]["gross_profit"] == "240.00"
    assert p[0]["quantity_sold"] == "3.0000"
    assert p[0]["cost_basis"] == "calculated"


def test_profit_is_labelled_estimated_without_cost_basis(api):
    _txn(api, "sale", 100_000)
    _txn(api, "purchase", 60_000, description="market goods")

    o = api.get("/analytics/overview", params=OCT).json()
    assert _v(o, "cost_of_goods_sold") == "60000.00"
    assert o["cost_of_goods_sold"]["method"] == "purchases_in_period"
    assert o["net_profit"]["basis"] == "estimated"
    assert o["confidence"]["level"] != "high"
    assert any("estimated" in r for r in o["confidence"]["reasons"])


def test_no_purchase_data_is_low_confidence(api):
    _txn(api, "sale", 100_000)
    o = api.get("/analytics/overview", params=OCT).json()
    assert o["confidence"]["level"] == "low"
    assert o["net_profit"]["basis"] == "estimated"


def test_receivables_profit_vs_cash(api):
    """PRD §11: profit is not cash. Credit sales create receivables."""
    _txn(api, "sale", 300_000, settlement="credit", counterparty_name="Chidi Stores")
    _txn(api, "sale", 200_000)
    _txn(api, "customer_payment", 100_000, counterparty_name="Chidi Stores")

    o = api.get("/analytics/overview", params=OCT).json()
    assert _v(o, "revenue") == "500000.00"  # customer_payment is not new revenue
    assert _v(o, "cash_in") == "300000.00"
    assert _v(o, "receivables") == "200000.00"


def test_period_comparison_and_trends(api):
    _txn(api, "sale", 100, "2026-09-15")
    _txn(api, "sale", 150, "2026-10-15")
    _txn(api, "expense", 40, "2026-10-16", category="Transport")

    o = api.get("/analytics/overview", params=OCT).json()
    assert o["previous_period"] == {"start": "2026-09-01", "end": "2026-09-30"}
    assert o["change_vs_previous_pct"]["revenue"] == "50.0"

    t = api.get(
        "/analytics/trends",
        params={"start": "2026-09-01", "end": "2026-10-31", "granularity": "month"},
    ).json()
    assert [(s["period_start"], s["revenue"]) for s in t["series"]] == [
        ("2026-09-01", "100.00"),
        ("2026-10-01", "150.00"),
    ]

    e = api.get("/analytics/expenses", params=OCT).json()
    assert e["categories"][0]["category"] == "transport"
    assert e["total"] == "40.00"


def test_provisional_excluded_until_confirmed(api, client):
    _txn(api, "sale", 1000)
    batch = api.post(
        "/imports", files={"file": ("x.csv", b"Date,Details,Amount\n10/10/2026,keke,500\n")}
    ).json()
    api.post(f"/imports/{batch['id']}/commit")

    o = api.get("/analytics/overview", params=OCT).json()
    assert _v(o, "operating_expenses") == "0.00"
    assert o["data"]["provisional_excluded"] == 1

    with_prov = api.get("/analytics/overview", params={**OCT, "include_provisional": True}).json()
    assert _v(with_prov, "operating_expenses") == "500.00"

    prov = api.get("/transactions", params={"status": "provisional"}).json()["items"][0]
    assert api.post(f"/transactions/{prov['id']}/confirm").status_code == 200
    o2 = api.get("/analytics/overview", params=OCT).json()
    assert _v(o2, "operating_expenses") == "500.00"
    assert o2["data"]["provisional_excluded"] == 0
