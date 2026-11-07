"""Offline tests for /v1/jobs and /v1/info: upstream calls are replaced with canned payloads."""
import pytest
from fastapi.testclient import TestClient

from app import info, jobs
from app.main import app

H = {"X-API-Key": "testkey"}

REMOTIVE = {"total-job-count": 2, "jobs": [
    {"id": 1, "url": "https://remotive.com/j/1", "title": "Python Developer", "company_name": "Acme", "category": "Software Development",
     "tags": ["python", "django"], "job_type": "full_time", "publication_date": "2026-11-05T10:00:00", "candidate_required_location": "Worldwide",
     "salary": "$90k-$120k", "description": "<p>Build <b>APIs</b></p>"},
    {"id": 2, "url": "https://remotive.com/j/2", "title": "Python Developer", "company_name": "Acme", "category": "Software Development",
     "tags": [], "job_type": "contract", "publication_date": "2026-11-04T10:00:00", "candidate_required_location": "USA", "description": ""}]}
REMOTEOK = [{"legal": "x"}, {"id": "9", "position": "Go Engineer", "company": "Beta", "url": "https://remoteok.com/9", "tags": ["go"],
                              "date": "2026-11-06T08:00:00+00:00", "salary_min": 100000, "salary_max": 140000, "description": "<p>Go</p>"}]


@pytest.fixture()
def client(monkeypatch):
    async def fake(url, params=None, ttl=0):
        if "remotive.com/api/remote-jobs/categories" in url:
            return {"jobs": [{"slug": "dev", "name": "Dev"}]}
        if "remotive" in url:
            return REMOTIVE
        if "remoteok" in url:
            return REMOTEOK
        raise jobs.HTTPException(502, "down")  # every other board is "down"
    monkeypatch.setattr(jobs, "_get_json", fake)
    return TestClient(app, raise_server_exceptions=False)


def test_requires_key(client):
    assert client.get("/v1/jobs/search").status_code == 401


def test_search_merges_dedupes_and_reports_failed_boards(client):
    r = client.get("/v1/jobs/search", params={"q": "developer", "source": "all"}, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert [j["title"] for j in d["jobs"]] == ["Python Developer"]  # duplicate title+company collapsed
    assert "arbeitnow" in d["errors"] and "remotive" not in d["errors"]


def test_salary_filter_and_normalisation(client):
    d = client.get("/v1/jobs/search", params={"source": "remoteok", "min_salary": 120000}, headers=H).json()
    assert d["count"] == 1 and d["jobs"][0]["salary"]["annual_max"] == 140000 and d["jobs"][0]["id"] == "remoteok:9"
    assert client.get("/v1/jobs/search", params={"source": "remoteok", "min_salary": 200000}, headers=H).json()["count"] == 0


def test_local_keyword_filter_on_boards_without_search(client):
    assert client.get("/v1/jobs/search", params={"q": "golang", "source": "remoteok"}, headers=H).json()["count"] == 0
    assert client.get("/v1/jobs/search", params={"q": "go", "source": "remoteok"}, headers=H).json()["count"] == 1


def test_description_preview_vs_full(client):
    j = client.get("/v1/jobs/search", params={"source": "remotive", "description": "true"}, headers=H).json()["jobs"][0]
    assert j["description"] == "Build APIs"


def test_unknown_source_and_all_down(client):
    assert client.get("/v1/jobs/search", params={"source": "nope"}, headers=H).status_code == 400
    assert client.get("/v1/jobs/search", params={"source": "themuse"}, headers=H).status_code == 502


def test_insights_and_categories(client):
    d = client.get("/v1/jobs/insights", params={"q": "go", "source": "remoteok"}, headers=H).json()
    assert d["openings_found"] == 1 and d["salary"]["median"] == 120000
    assert client.get("/v1/jobs/categories", params={"source": "remotive"}, headers=H).json()["categories"][0]["slug"] == "dev"


def test_text_cleaner():
    assert jobs._text("<p>Hi&nbsp;<b>there</b></p><script>x</script><br>bye") == "Hi there\nbye"


def test_info_validation():
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/v1/info/ip", params={"ip": "127.0.0.1"}, headers=H).status_code == 400
    assert c.get("/v1/info/ip", params={"ip": "10.0.0.5"}, headers=H).status_code == 400
    assert c.get("/v1/info/ip", params={"ip": "nope"}, headers=H).status_code == 400
    assert c.get("/v1/info/weather", headers=H).status_code == 400
    assert c.get("/v1/info/crypto", params={"ids": "bitcoin;drop"}, headers=H).status_code == 400
