import uuid
from datetime import timedelta

from sqlalchemy import update

from app.models import Invitation
from app.models.base import utcnow

OCT = {"start": "2026-10-01", "end": "2026-10-31"}


def _register(client, email: str) -> dict[str, str]:
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct-horse-1", "full_name": email.split("@")[0]},
    )
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _invite(api, email: str, role: str = "member", headers=None, client=None, expect=201):
    if headers is None:
        res = api.post("/invitations", json={"email": email, "role": role})
    else:
        res = client.post(
            f"{api.base}/invitations", json={"email": email, "role": role}, headers=headers
        )
    assert res.status_code == expect, res.text
    return res.json()


def join(client, api, role: str) -> tuple[dict[str, str], str]:
    """Invite a new person with `role`, have them sign up and accept. Returns (headers, id)."""
    email = f"{role}-{uuid.uuid4().hex[:6]}@example.com"
    token = _invite(api, email, role)["token"]
    headers = _register(client, email)
    res = client.post("/v1/invitations/accept", json={"token": token}, headers=headers)
    assert res.status_code == 200, res.text
    user_id = client.get("/v1/auth/me", headers=headers).json()["id"]
    return headers, user_id


def _txn(kind="sale", amount="1000", **extra):
    return {"kind": kind, "amount": amount, "occurred_on": "2026-10-05", **extra}


# --------------------------------------------------------------------------- invite lifecycle


def test_invite_preview_accept(client, api):
    inv = _invite(api, "Bola@Example.com", "member")
    assert inv["email"] == "bola@example.com"
    assert inv["invite_link"].endswith(f"/invite/{inv['token']}")

    preview = client.post("/v1/invitations/preview", json={"token": inv["token"]})
    assert preview.status_code == 200  # no login needed
    assert preview.json()["business_name"] == "Mama Nkechi Provisions"
    assert preview.json()["role"] == "member"
    assert preview.json()["invited_by_name"] == "Ada Obi"

    bola = _register(client, "bola@example.com")
    joined = client.post("/v1/invitations/accept", json={"token": inv["token"]}, headers=bola)
    assert joined.status_code == 200
    assert joined.json()["role"] == "member"

    members = api.get("/members").json()
    assert sorted(m["role"] for m in members) == ["member", "owner"]
    assert api.get("/invitations").json() == []  # nothing pending anymore

    # Single use.
    again = client.post("/v1/invitations/accept", json={"token": inv["token"]}, headers=bola)
    assert again.status_code == 410

    actions = [e["action"] for e in api.get("/audit-log").json()]
    assert actions == ["member.joined", "invitation.created"]


def test_forwarded_link_cant_be_used_by_someone_else(client, api):
    inv = _invite(api, "bola@example.com")
    stranger = _register(client, "stranger@example.com")
    res = client.post("/v1/invitations/accept", json={"token": inv["token"]}, headers=stranger)
    assert res.status_code == 403
    assert "bo**@example.com" in res.json()["detail"]  # masked
    # The real invitee can still use it.
    bola = _register(client, "bola@example.com")
    assert (
        client.post(
            "/v1/invitations/accept", json={"token": inv["token"]}, headers=bola
        ).status_code
        == 200
    )


def test_expired_revoked_and_resent_invites(client, api, db):
    bola = _register(client, "bola@example.com")

    first = _invite(api, "bola@example.com")
    second = _invite(api, "bola@example.com")  # re-invite = resend; old link stops working
    assert (
        client.post(
            "/v1/invitations/accept", json={"token": first["token"]}, headers=bola
        ).status_code
        == 410
    )
    assert len(api.get("/invitations").json()) == 1

    assert api.post(f"/invitations/{second['id']}/revoke").status_code == 200
    assert (
        client.post(
            "/v1/invitations/accept", json={"token": second["token"]}, headers=bola
        ).status_code
        == 410
    )

    third = _invite(api, "bola@example.com")
    db.execute(
        update(Invitation)
        .where(Invitation.id == third["id"])
        .values(expires_at=utcnow() - timedelta(minutes=1))
    )
    db.commit()
    res = client.post("/v1/invitations/accept", json={"token": third["token"]}, headers=bola)
    assert res.status_code == 410
    assert "expired" in res.json()["detail"]

    assert client.post("/v1/invitations/preview", json={"token": "x" * 40}).status_code == 404


def test_cant_invite_existing_member_or_as_owner(client, api):
    _invite(api, "x@example.com", "owner", expect=403)
    member, _ = join(client, api, "member")
    email = client.get("/v1/auth/me", headers=member).json()["email"]
    _invite(api, email, "viewer", expect=409)


# --------------------------------------------------------------------------- permissions


def test_member_records_but_cant_see_financials_or_personal(client, api):
    api.post("/transactions", json=_txn())
    withdrawal = api.post("/transactions", json=_txn("owner_withdrawal", "50000")).json()

    staff, _ = join(client, api, "member")
    base = api.base

    # Can record.
    assert client.post(f"{base}/transactions", json=_txn(), headers=staff).status_code == 201
    took = client.post(f"{base}/transactions", json=_txn("owner_withdrawal", "5000"), headers=staff)
    assert took.status_code == 201  # they get their own entry back once...

    # ...but never see the owner's personal records afterwards.
    listed = client.get(f"{base}/transactions", headers=staff).json()
    assert listed["total"] == 2
    assert all(t["kind"] == "sale" for t in listed["items"])
    assert client.get(f"{base}/transactions/{withdrawal['id']}", headers=staff).status_code == 404
    assert (
        client.post(
            f"{base}/transactions/{took.json()['id']}/void", json={}, headers=staff
        ).status_code
        == 404
    )

    # No profit, analytics or team management.
    for path in (
        "/analytics/overview",
        "/dashboard",
        "/insights",
        "/brief",
        "/health-score",
        "/audit-log",
        "/invitations",
    ):
        assert client.get(base + path, headers=staff).status_code == 403, path
    assert client.patch(base, json={"name": "Hijacked"}, headers=staff).status_code == 403
    _invite(api, "friend@example.com", headers=staff, client=client, expect=403)

    access = client.get(f"{base}/me/access", headers=staff).json()
    assert access == {"role": "member", "permissions": ["record"]}

    # Owner still sees everything: 2 sales + 2 withdrawals.
    assert api.get("/transactions").json()["total"] == 4


def test_viewer_is_read_only(client, api):
    api.post("/transactions", json=_txn("owner_withdrawal", "50000"))
    viewer, _ = join(client, api, "viewer")
    base = api.base

    assert client.get(f"{base}/analytics/overview", params=OCT, headers=viewer).status_code == 200
    assert client.get(f"{base}/transactions", headers=viewer).json()["total"] == 1  # sees personal
    assert client.post(f"{base}/transactions", json=_txn(), headers=viewer).status_code == 403
    assert (
        client.post(
            f"{base}/stock-counts",
            json={
                "counted_on": "2026-10-05",
                "items": [{"product_name": "Rice", "counted_qty": 1}],
            },
            headers=viewer,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/imports",
            files={"file": ("x.csv", b"Date,Amount\n01/10/2026,5\n")},
            headers=viewer,
        ).status_code
        == 403
    )


def test_admin_manages_staff_but_not_admins_or_owner(client, api):
    admin, admin_id = join(client, api, "admin")
    _, other_admin_id = join(client, api, "admin")
    _, staff_id = join(client, api, "member")
    owner_id = next(m["user_id"] for m in api.get("/members").json() if m["role"] == "owner")
    base = api.base

    assert _invite(api, "s@example.com", "member", headers=admin, client=client)["role"] == "member"
    _invite(api, "a@example.com", "admin", headers=admin, client=client, expect=403)

    def patch(uid, role):
        return client.patch(f"{base}/members/{uid}", json={"role": role}, headers=admin)

    assert patch(staff_id, "viewer").status_code == 200
    assert patch(staff_id, "admin").status_code == 403  # can't promote to admin
    assert patch(other_admin_id, "member").status_code == 403  # can't demote an admin
    assert patch(owner_id, "member").status_code == 403
    assert patch(admin_id, "member").status_code == 403  # not even yourself

    assert client.delete(f"{base}/members/{owner_id}", headers=admin).status_code == 403
    assert client.delete(f"{base}/members/{other_admin_id}", headers=admin).status_code == 403
    assert client.delete(f"{base}/members/{staff_id}", headers=admin).status_code == 204

    changed = next(e for e in api.get("/audit-log").json() if e["action"] == "member.role_changed")
    assert changed["details"] == {"old_role": "member", "new_role": "viewer"}


def test_leaving_and_ownership_transfer(client, api, auth):
    staff, staff_id = join(client, api, "member")
    admin, admin_id = join(client, api, "admin")
    owner_id = client.get("/v1/auth/me", headers=auth).json()["id"]
    base = api.base

    # Anyone but the owner can leave.
    assert client.delete(f"{base}/members/{staff_id}", headers=staff).status_code == 204
    assert client.get(base, headers=staff).status_code == 404
    assert client.delete(f"{base}/members/{owner_id}", headers=auth).status_code == 403

    # Role changes can't create a second owner.
    assert api.patch(f"/members/{admin_id}", json={"role": "owner"}).status_code == 403
    # Admins can't transfer ownership.
    assert (
        client.post(
            f"{base}/transfer-ownership", json={"user_id": admin_id}, headers=admin
        ).status_code
        == 403
    )

    res = api.post("/transfer-ownership", json={"user_id": admin_id})
    assert res.status_code == 200
    roles = {m["user_id"]: m["role"] for m in res.json()}
    assert roles == {owner_id: "admin", admin_id: "owner"}

    # Old owner is now an admin: no business settings anymore.
    assert client.patch(base, json={"name": "New name"}, headers=auth).status_code == 403
    assert client.patch(base, json={"name": "New name"}, headers=admin).status_code == 200
