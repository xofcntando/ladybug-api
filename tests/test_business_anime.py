import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"X-API-Key": "testkey"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def get(client, path, **params):
    return client.get("/v1" + path, params=params, headers=H)


def test_vat_add_and_remove(client):
    r = get(client, "/business/vat", amount=100, country="HU").json()
    assert (r["net"], r["vat"], r["gross"]) == (100.0, 27.0, 127.0)
    r = get(client, "/business/vat", amount=127, rate=27, mode="remove").json()
    assert (r["net"], r["vat"]) == (100.0, 27.0)
    assert get(client, "/business/vat", amount=10).status_code == 400
    assert get(client, "/business/vat", amount=10, country="ZZ").status_code == 404


def test_pricing_and_discount(client):
    r = get(client, "/business/pricing", cost=60, margin=40).json()
    assert r["price"] == 100.0 and r["markup_pct"] == 66.67
    r = get(client, "/business/pricing", cost=50, markup=100).json()
    assert r["price"] == 100.0 and r["margin_pct"] == 50.0
    r = get(client, "/business/discount", price=100, percent="20,10").json()
    assert r["final"] == 72.0 and r["effective_percent_off"] == 28.0


def test_break_even_roi_loan(client):
    r = get(client, "/business/break-even", fixed_costs=1000, price=30, variable_cost=10).json()
    assert r["units_needed"] == 50 and r["revenue_needed"] == 1500.0
    assert get(client, "/business/break-even", fixed_costs=1, price=5, variable_cost=9).status_code == 400
    assert get(client, "/business/roi", invested=1000, returned=1500, years=2).json()["roi_pct"] == 50.0
    r = get(client, "/business/loan", principal=12000, annual_rate=0, months=12, schedule=True).json()
    assert r["monthly_payment"] == 1000.0 and r["schedule"][-1]["balance"] == 0.0
    r = get(client, "/business/loan", principal=100000, annual_rate=6, months=360).json()
    assert r["monthly_payment"] == 599.55


def test_saas_and_reorder_and_volumetric(client):
    r = get(client, "/business/saas-metrics", customers=100, mrr=5000, churned=5, new_customers=10, marketing_spend=2000).json()
    assert r["arr"] == 60000.0 and r["churn_rate_pct"] == 5.0 and r["cac"] == 200.0
    r = get(client, "/business/reorder", daily_demand=10, lead_time_days=7, safety_days=3).json()
    assert r["reorder_point"] == 100 and r["safety_stock"] == 30
    r = get(client, "/business/volumetric", length_cm=50, width_cm=40, height_cm=30, weight_kg=5).json()
    assert r["volumetric_kg"] == 12.0 and r["billed_by"] == "volume"


def test_validators(client):
    assert get(client, "/business/gtin", code="4006381333931").json()["valid"] is True
    assert get(client, "/business/gtin", code="4006381333932").json()["valid"] is False
    assert get(client, "/business/gtin", code="036000291452").json()["type"] == "UPC-A"
    assert get(client, "/business/gtin", code="400638133393", generate="true").json()["full_code"] == "4006381333931"
    assert get(client, "/business/iban", iban="GB82 WEST 1234 5698 7654 32").json()["valid"] is True
    assert get(client, "/business/iban", iban="GB82WEST12345698765433").json()["valid"] is False
    r = get(client, "/business/card", number="4111 1111 1111 1111").json()
    assert r["valid"] and r["brand"] == "visa" and r["masked"].endswith("1111")
    assert get(client, "/business/vat-id", number="HU12345678").json()["valid_format"] is True
    assert get(client, "/business/vat-id", number="DE12345").json()["valid_format"] is False


def test_utm_and_business_days(client):
    u = get(client, "/business/utm", url="example.com/p?a=1", source="News Letter", medium="email", campaign="Black Friday").json()["url"]
    assert u == "https://example.com/p?a=1&utm_source=news-letter&utm_medium=email&utm_campaign=black-friday"
    # Fri 2026-11-06 + 1 business day = Mon 2026-11-09; a holiday pushes it to Tue
    assert get(client, "/business/business-days", start="2026-11-06", days=1).json()["result"] == "2026-11-09"
    assert get(client, "/business/business-days", start="2026-11-06", days=1, holidays="2026-11-09").json()["result"] == "2026-11-10"
    assert get(client, "/business/business-days", start="2026-11-02", end="2026-11-09").json()["business_days"] == 5


INVOICE = {"number": "INV-1", "seller": {"name": "Acme <Ltd>"}, "buyer": {"name": "Bob"}, "currency": "eur", "tax_rate": 20,
           "issue_date": "2026-11-01", "due_days": 10,
           "items": [{"description": "Widget", "quantity": 2, "unit_price": 50}, {"description": "Book", "unit_price": 10, "tax_rate": 0}]}


def test_invoice(client):
    r = client.post("/v1/business/invoice", json=INVOICE, headers=H).json()
    assert (r["subtotal"], r["tax_total"], r["total"]) == (110.0, 20.0, 130.0)
    assert r["due_date"] == "2026-11-11" and r["currency"] == "EUR"
    assert "Acme &lt;Ltd&gt;" in r["html"] and "<Ltd>" not in r["html"]  # HTML is escaped
    inc = client.post("/v1/business/invoice", json={**INVOICE, "prices_include_tax": True, "discount_pct": 10}, headers=H).json()
    assert inc["total"] == round(inc["subtotal"] + inc["tax_total"], 2)
    h = client.post("/v1/business/invoice?format=html", json=INVOICE, headers=H)
    assert h.headers["content-type"].startswith("text/html")


@pytest.fixture
def fake_anilist(monkeypatch):
    calls = []

    async def fake(query, variables, ttl=600):
        calls.append((query, variables))
        if "recommendations" in query:
            return {"Media": {"id": 1, "title": {"romaji": "A", "english": "Alpha"}, "recommendations": {"nodes": [
                {"rating": 9, "mediaRecommendation": {"id": 2, "title": {"romaji": "B", "english": None}, "type": "ANIME"}},
                {"rating": 1, "mediaRecommendation": None}]}}}
        if "GenreCollection" in query:
            return {"GenreCollection": ["Action", "Hentai", "Comedy"]}
        if "MediaTagCollection" in query:
            return {"MediaTagCollection": [{"name": "Isekai", "description": "d", "category": "Setting", "isAdult": False},
                                           {"name": "Nope", "description": "d", "category": "Sexual", "isAdult": True}]}
        if "Page(page" in query:
            return {"Page": {"pageInfo": {"total": 1, "hasNextPage": False}, "media": [{"id": 3, "title": {"romaji": "C"}}]}}
        if "relations" in query:
            return {"Media": {"id": 1, "title": {"romaji": "A"}, "relations": {"edges": [
                {"relationType": "SEQUEL", "node": {"id": 4, "type": "ANIME", "title": {"romaji": "D"}}}]}}}
        raise AssertionError(query)
    monkeypatch.setattr("app.anime_plus.anilist", fake)
    return calls


def test_anime_plus(client, fake_anilist):
    r = get(client, "/anime/recommend", q="Alpha").json()
    assert r["title"] == "Alpha" and r["count"] == 1 and r["results"][0]["title"] == "B" and r["results"][0]["votes"] == 9
    assert get(client, "/anime/recommend").status_code == 400
    assert get(client, "/anime/genres").json()["genres"] == ["Action", "Comedy"]
    assert [t["name"] for t in get(client, "/anime/tags").json()["tags"]] == ["Isekai"]
    r = get(client, "/anime/discover", genre="Action, Comedy", year=2024, min_score=75).json()
    assert r["total"] == 1 and fake_anilist[-1][1]["g"] == ["Action", "Comedy"] and fake_anilist[-1][1]["sc"] == 74
    assert get(client, "/anime/discover", genre="hentai").status_code == 400
    assert get(client, "/anime/relations", id=1).json()["relations"][0]["relation"] == "SEQUEL"
