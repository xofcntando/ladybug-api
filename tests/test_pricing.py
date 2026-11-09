import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from app import pricing
    monkeypatch.setattr(pricing, "USER", "Ntando")
    monkeypatch.setattr(pricing, "PASS_HASH", pricing.hash_password("secret-pw"))
    monkeypatch.setattr(pricing, "SECRET", "test-secret")
    pricing._fails.clear()
    from app.main import app
    return TestClient(app, base_url="https://testserver")


def test_public_pricing_has_free_50(client):
    d = client.get("/v1/pricing").json()
    free = next(p for p in d["plans"] if p["id"] == "free")
    assert free["price_monthly"] == 0 and free["requests_per_month"] == 50


def test_pages_load(client):
    assert client.get("/pricing").status_code == 200
    assert client.get("/pricing-admin").status_code == 200


def test_login_flow_and_session(client):
    assert client.get("/v1/pricing/admin/me").status_code == 401
    assert client.post("/v1/pricing/admin/login", json={"username": "Ntando", "password": "nope"}).status_code == 401
    r = client.post("/v1/pricing/admin/login", json={"username": "ntando", "password": "secret-pw"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/v1/pricing/admin/me").json()["user"] == "Ntando"


def test_edit_needs_login_and_mongo(client):
    body = {"currency": "USD", "plans": [{"id": "x", "name": "X", "price_monthly": 1, "requests_per_month": 5}]}
    assert client.put("/v1/pricing/admin/plans", json=body).status_code == 401
    client.post("/v1/pricing/admin/login", json={"username": "Ntando", "password": "secret-pw"})
    assert client.put("/v1/pricing/admin/plans", json=body).status_code == 503  # no MongoDB in tests


def test_lockout_after_failures(client):
    for _ in range(5):
        client.post("/v1/pricing/admin/login", json={"username": "Ntando", "password": "bad"})
    r = client.post("/v1/pricing/admin/login", json={"username": "Ntando", "password": "secret-pw"})
    assert r.status_code == 429


def test_session_cannot_open_cookie_admin(client):
    client.post("/v1/pricing/admin/login", json={"username": "Ntando", "password": "secret-pw"})
    assert client.get("/v1/admin/storage").status_code in (403, 503)


def test_forged_cookie_rejected(client):
    client.cookies.set("lb_pricing", "Ntando|9999999999|deadbeef")
    assert client.get("/v1/pricing/admin/me").status_code == 401
