"""KASU-002: every business endpoint is closed to outsiders, and refused attempts are logged."""

import re
import uuid

import pytest
from sqlalchemy import select

from app.main import app
from app.models import AccessDenial
from tests.conftest import register
from tests.test_team import join

_PARAM = re.compile(r"\{(\w+)\}")


def _business_routes() -> list[tuple[str, str]]:
    """Every documented endpoint under a business, from the OpenAPI schema."""
    return [
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if "{business_id}" in path
        for method in sorted(ops)
    ]


BUSINESS_ROUTES = _business_routes()


def test_route_inventory_covers_the_sensitive_areas():
    """Guard against the parametrised test below silently checking nothing."""
    paths = {path for _, path in BUSINESS_ROUTES}
    for needle in ("/transactions", "/imports", "/analytics/", "/brief", "/members", "/stock"):
        assert any(needle in p for p in paths), needle
    assert len(BUSINESS_ROUTES) > 30


@pytest.mark.parametrize(("method", "path"), BUSINESS_ROUTES)
def test_outsider_is_refused_everywhere(client, business, db, method, path):
    outsider = register(client)
    url = _PARAM.sub(lambda m: business if m.group(1) == "business_id" else str(uuid.uuid4()), path)
    res = client.request(method, url, headers=outsider, json={})
    # 404 rather than 403, so outsiders can't even learn the business exists.
    assert res.status_code == 404, (method, path, res.text)
    assert res.json() == {"detail": "Business not found"}

    logged = db.scalars(select(AccessDenial)).all()
    assert len(logged) == 1
    assert logged[0].reason == "not_a_member"
    assert str(logged[0].business_id) == business
    assert logged[0].method == method


def test_refusals_are_logged_and_owner_can_review_them(client, api, business, db):
    base = f"/v1/businesses/{business}"

    # Not signed in, a forged token, and a wrong password.
    assert client.get(f"{base}/transactions").status_code == 401
    forged = {"Authorization": "Bearer not-a-real-token"}
    assert client.get(f"{base}/transactions", headers=forged).status_code == 401
    bad_login = client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": "guess-guess"}
    )
    assert bad_login.status_code == 401
    assert bad_login.json() == {"detail": "Incorrect email or password"}

    # A staff member trying things their role doesn't allow.
    staff, staff_id = join(client, api, "member")
    denied = client.get(f"{base}/analytics/overview", headers=staff)
    assert denied.status_code == 403
    assert denied.json() == {"detail": "Your role (member) can't do this"}
    assert (
        client.post(
            f"{base}/invitations", json={"email": "x@example.com", "role": "admin"}, headers=staff
        ).status_code
        == 403
    )
    # Team rules enforced in the service layer are logged too.
    admin, _ = join(client, api, "admin")
    assert (
        client.post(
            f"{base}/invitations", json={"email": "y@example.com", "role": "admin"}, headers=admin
        ).status_code
        == 403
    )

    reasons = [d.reason for d in db.scalars(select(AccessDenial))]
    assert sorted(reasons) == sorted(
        [
            "missing_token",
            "invalid_token",
            "login_failed",
            "missing_permission",
            "missing_permission",
            "team_rule",
        ]
    )

    # The owner sees what happened to *their* business (not the unrelated failed login).
    log = api.get("/access-denials").json()
    assert {d["reason"] for d in log} == {
        "missing_token",
        "invalid_token",
        "missing_permission",
        "team_rule",
    }
    by_staff = [d for d in log if d["user_id"] == staff_id]
    assert {d["path"] for d in by_staff} == {f"{base}/analytics/overview", f"{base}/invitations"}

    # Staff can't read the log (and that attempt is itself logged).
    assert client.get(f"{base}/access-denials", headers=staff).status_code == 403
    assert len(api.get("/access-denials").json()) == len(log) + 1


def test_inactive_user_is_refused(client, auth, business, db):
    from app.models import User

    user = db.scalars(select(User)).one()
    user.is_active = False
    db.commit()
    assert client.get(f"/v1/businesses/{business}", headers=auth).status_code == 401
    denial = db.scalars(select(AccessDenial)).one()
    assert denial.reason == "inactive_user" and denial.user_id == user.id
