"""KASU-030: weekly generation, delivery, insufficient-data prompts and open-rate tracking."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, update

from app.models import Business, WeeklyBrief
from app.services.briefs import last_completed_week_end, open_rate, run_weekly_briefs
from tests.conftest import register
from tests.test_team import join

TODAY = date.today()
WEEK_END = last_completed_week_end(TODAY)
WEEK_START = WEEK_END - timedelta(days=6)


def _post(client, base, headers, kind, amount, day: date, **extra):
    res = client.post(
        f"{base}/transactions",
        headers=headers,
        json={"kind": kind, "amount": str(amount), "occurred_on": day.isoformat(), **extra},
    )
    assert res.status_code == 201, res.text


def _busy_week(client, api):
    """Enough for a real brief: last week's sales to compare against, then a full week."""
    post = lambda *a, **kw: _post(client, api.base, api.headers, *a, **kw)  # noqa: E731
    for i in range(3):
        post("sale", 50_000, WEEK_START - timedelta(days=6 - i))
    post("purchase", 300_000, WEEK_START, product_name="Rice", quantity="5")
    for i in range(4):
        post("sale", 80_000, WEEK_START + timedelta(days=i + 1), product_name="Rice", quantity="1")
    post("expense", 5_000, WEEK_START + timedelta(days=2), category="transport")


def _backdate(db, business_id, days: int):
    db.execute(
        update(Business)
        .where(Business.id == business_id)
        .values(created_at=Business.created_at - timedelta(days=days))
    )
    db.commit()


def _new_business(client, name: str):
    headers = register(client)
    res = client.post("/v1/businesses", json={"name": name}, headers=headers)
    return headers, res.json()["id"]


def test_last_completed_week():
    assert last_completed_week_end(date(2026, 11, 16)) == date(2026, 11, 15)  # Monday
    assert last_completed_week_end(date(2026, 11, 18)) == date(2026, 11, 15)  # Wednesday
    assert last_completed_week_end(date(2026, 11, 15)) == date(2026, 11, 8)  # Sunday: not over


def test_weekly_job_generates_delivers_and_tracks_opens(client, api, business, db):
    viewer, viewer_id = join(client, api, "viewer")
    staff, _ = join(client, api, "member")
    _busy_week(client, api)

    thin_headers, thin = _new_business(client, "Thin Records Ltd")
    _post(client, f"/v1/businesses/{thin}", thin_headers, "sale", 2_000, WEEK_START)
    _, dormant = _new_business(client, "Dormant Stores")
    for biz, days in ((business, 30), (thin, 30), (dormant, 200)):
        _backdate(db, biz, days)

    summary = run_weekly_briefs(db, today=TODAY)
    assert summary.week_end == WEEK_END
    assert (summary.businesses, summary.ready, summary.insufficient_data) == (2, 1, 1)
    assert summary.failed == []

    # Idempotent: a second run (or a second runner) sends nothing new.
    again = run_weekly_briefs(db, today=TODAY)
    assert (again.already_generated, again.ready, again.insufficient_data) == (2, 0, 0)
    assert len(db.scalars(select(WeeklyBrief)).all()) == 2

    # Delivered in-app to people who may see financials; staff get nothing.
    inbox = client.get("/v1/notifications", headers=api.headers).json()
    assert [n["title"] for n in inbox] == ["Your week at Mama Nkechi Provisions"]
    assert inbox[0]["body"].startswith("Sales were ₦320,000")
    assert inbox[0]["opened_at"] is None
    assert len(client.get("/v1/notifications", headers=viewer).json()) == 1
    assert client.get("/v1/notifications", headers=staff).json() == []

    # The thin business gets a prompt, not a brief.
    (prompt,) = client.get("/v1/notifications", headers=thin_headers).json()
    assert prompt["title"] == "Add more records to get your weekly brief"
    assert "Record at least 3 transactions" in prompt["body"]

    stats = api.get("/briefs/stats").json()
    assert (stats["delivered"], stats["opened"], stats["open_rate_pct"]) == (2, 0, "0.0")

    (listed,) = api.get("/briefs").json()
    assert listed["status"] == "ready" and listed["opened_by_me"] is False
    assert listed["week_end"] == WEEK_END.isoformat()

    brief = api.get(f"/briefs/{listed['id']}").json()
    assert brief["status"] == "ready"
    assert brief["highlights"], brief
    for line in brief["highlights"]:
        assert line["text"] and line["evidence"]
        assert line["confidence"] in ("high", "medium", "low")
        assert line["basis"] in ("calculated", "estimated")
    api.get(f"/briefs/{listed['id']}")  # opening twice counts once

    stats = api.get("/briefs/stats").json()
    assert (stats["opened"], stats["open_rate_pct"]) == (1, "50.0")
    assert api.get("/briefs").json()[0]["opened_by_me"] is True

    # Opening from the notification counts as well.
    opened = client.post(f"/v1/notifications/{inbox[0]['id']}/open", headers=viewer)
    assert opened.status_code == 404  # not the viewer's notification
    viewer_note = client.get("/v1/notifications", headers=viewer).json()[0]
    opened = client.post(f"/v1/notifications/{viewer_note['id']}/open", headers=viewer)
    assert opened.status_code == 200 and opened.json()["opened_at"] is not None
    assert api.get("/briefs/stats").json()["open_rate_pct"] == "100.0"

    platform = open_rate(db)
    assert platform["delivered"] == 3 and platform["opened"] == 2
    assert platform["by_status"]["insufficient_data"] == {
        "delivered": 1,
        "opened": 0,
        "open_rate_pct": Decimal("0.0"),
    }

    # Staff can't read briefs; people who leave stop seeing the notifications.
    assert client.get(f"{api.base}/briefs/{listed['id']}", headers=staff).status_code == 403
    assert client.delete(f"{api.base}/members/{viewer_id}", headers=api.headers).status_code == 204
    assert client.get("/v1/notifications", headers=viewer).json() == []


def test_on_demand_brief_refuses_to_mislead_on_thin_data(client, api):
    _post(client, api.base, api.headers, "sale", 10_000, WEEK_START)
    _post(client, api.base, api.headers, "expense", 2_000, WEEK_START, category="transport")
    b = api.get("/brief", params={"week_ending": WEEK_END.isoformat()}).json()
    assert b["status"] == "insufficient_data"
    assert b["highlights"] == [] and b["recommendations"] == []
    assert b["prompt"].startswith("Only 2 transaction(s) were recorded this week.")
    assert "Sales were" not in b["text"]
