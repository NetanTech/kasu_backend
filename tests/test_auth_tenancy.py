from tests.conftest import register


def test_register_login_me(client):
    res = client.post(
        "/v1/auth/register",
        json={"email": "Ada@Example.com", "password": "correct-horse-1", "full_name": "Ada"},
    )
    assert res.status_code == 201

    dup = client.post(
        "/v1/auth/register",
        json={"email": "ada@example.com", "password": "correct-horse-1", "full_name": "Ada"},
    )
    assert dup.status_code == 409

    bad = client.post("/v1/auth/login", json={"email": "ada@example.com", "password": "nope-nope"})
    assert bad.status_code == 401

    ok = client.post(
        "/v1/auth/login", json={"email": "ADA@example.com", "password": "correct-horse-1"}
    )
    assert ok.status_code == 200
    tokens = ok.json()

    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json()["email"] == "ada@example.com"

    # A refresh token can't be used as an access token, and vice versa.
    wrong = client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert wrong.status_code == 401
    refreshed = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert (
        client.post("/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}).status_code
        == 401
    )


def test_requires_auth(client):
    assert client.get("/v1/businesses").status_code == 401


def test_business_isolation(client, auth, business):
    """Another user can't see or touch a business they're not a member of (PRD §32)."""
    other = register(client)
    base = f"/v1/businesses/{business}"

    assert client.get(base, headers=other).status_code == 404
    assert client.get(f"{base}/transactions", headers=other).status_code == 404
    assert (
        client.post(
            f"{base}/transactions",
            headers=other,
            json={"kind": "sale", "occurred_on": "2026-10-01", "amount": "100"},
        ).status_code
        == 404
    )
    assert client.get(f"{base}/analytics/overview", headers=other).status_code == 404

    mine = client.get("/v1/businesses", headers=auth).json()
    theirs = client.get("/v1/businesses", headers=other).json()
    assert [b["id"] for b in mine] == [business]
    assert theirs == []


def test_cannot_reference_another_tenants_product(client, auth, api):
    created = api.post(
        "/transactions",
        json={
            "kind": "purchase",
            "occurred_on": "2026-10-01",
            "amount": "100",
            "product_name": "Rice",
            "quantity": "1",
        },
    ).json()

    other = register(client)
    other_biz = client.post("/v1/businesses", json={"name": "Other"}, headers=other).json()["id"]
    res = client.post(
        f"/v1/businesses/{other_biz}/transactions",
        headers=other,
        json={
            "kind": "sale",
            "occurred_on": "2026-10-01",
            "amount": "100",
            "product_id": created["product_id"],
        },
    )
    assert res.status_code == 422
