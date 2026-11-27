"""App smoke tests against a seeded JSON store (no cloud, no network)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import core.store as store_mod
from app import main, queries
from core.urls import doc_id


@pytest.fixture()
def client(tmp_path, monkeypatch):
    s = store_mod.JsonStore(tmp_path)
    monkeypatch.setattr(store_mod, "_store", s)
    queries.clear_cache()
    today = date.today()
    now = datetime.now(timezone.utc).isoformat()
    urls = ["https://www.corvenia.com/", "https://www.corvenia.com/no-no/konsolidering"]
    for i, u in enumerate(urls):
        s.put("url_inventory", u, {
            "url": u, "loc": u, "path": u[24:] or "/", "first_seen": now, "precheck": {"ok": True},
            "google": {"status": "indexed"},
            "bing": {"status": "indexed" if i == 0 else "pending", "detail": "not crawled by Bing yet"}})
    for n in range(60):  # 60 days: current 28 = 2 clicks/day, previous 28 = 1 click/day
        d = (today - timedelta(days=n + 2)).isoformat()
        c = 2 if n < 28 else 1
        s.put("gsc_page_daily", doc_id(d, urls[0]), {"day": d, "url": urls[0], "clicks": c, "impressions": 10 * c, "language": "en"})
        s.put("gsc_daily", doc_id(d, "q"), {"day": d, "url": urls[0], "query": "konsolidering", "branded": False,
                                            "clicks": c, "impressions": 10 * c, "position": 9.0})
        s.put("gsc_daily", doc_id(d, "b"), {"day": d, "url": urls[0], "query": "corvenia", "branded": True,
                                            "clicks": 0, "impressions": 3, "position": 1.0})
        s.put("bing_daily", doc_id("site", d), {"day": d, "kind": "site", "clicks": 1, "impressions": 4})
    yield TestClient(main.app), s


def test_scorecard_numbers(client):
    c, _ = client
    sc = c.get("/api/scorecard?days=28").json()["tiles"]
    assert sc["google_clicks"]["value"] == 56 and sc["google_clicks"]["prev"] == 28
    assert sc["nonbranded_impressions"]["value"] == 560
    assert sc["bing_clicks"]["value"] == 28
    assert sc["indexed_google"]["value"] == 1.0 and sc["indexed_bing"]["value"] == 0.5


def test_clusters_and_opportunities(client):
    from core import clusters as cl

    _, s = client
    assert cl.classify("Konsolidering av konsernregnskap") == "consolidation"
    assert cl.classify("corvenia ab") == "brand"
    assert cl.classify("real-time intercompany eliminations") == "intercompany"
    assert cl.classify("weather oslo") == cl.UNCLASSIFIED
    opp = queries.opportunities(s, 28)
    assert [r["query"] for r in opp["rows"]] == ["konsolidering"]  # pos 9, non-brand; "corvenia" excluded
    assert opp["rows"][0]["cluster"] == "consolidation" and opp["rows"][0]["priority"]
    tiles = queries.scorecard(s, 28)["tiles"]
    assert tiles["priority_position"]["value"] == pytest.approx(9.0)
    perf = queries.performance(s, 28)["google"]["clusters"]
    assert {c["id"] for c in perf} == {"consolidation", "brand"}


def test_pages_render(client):
    c, _ = client
    for path in ("/", "/?days=7", "/coverage", "/performance?days=90", "/opportunities", "/opportunities?days=90", "/changes"):
        r = c.get(path)
        assert r.status_code == 200, path
    assert "Resubmit to Bing" not in c.get("/coverage").text  # submission is automatic; no manual button
    assert c.post("/api/resubmit", json={"url": "https://www.corvenia.com/"}).status_code in (404, 405)


def test_log_change_records_iap_user(client):
    c, s = client
    r = c.post("/api/changes", json={"url": "https://www.corvenia.com/", "type": "fix", "note": "Fixed title"},
               headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:a@b.c"})
    assert r.status_code == 200
    rows = s.query("changes")
    assert len(rows) == 1 and rows[0]["actor"] == "a@b.c" and rows[0]["type"] == "fix"


def test_exclusions_endpoints_and_denominator(client):
    c, s = client
    u = "https://www.corvenia.com/no-no/signup"
    s.put("url_inventory", u, {"url": u, "loc": u, "path": "/no-no/signup", "precheck": {"ok": True},
                               "google": {"status": "not_indexed", "coverage": "Excluded by 'noindex' tag"},
                               "bing": {"status": "pending"}})
    queries.clear_cache()
    sc = c.get("/api/scorecard?days=28").json()["tiles"]
    assert sc["indexed_google"]["total"] == 2 and sc["indexed_google"]["value"] == 1.0  # seed list excludes /signup
    page = c.get("/coverage").text
    assert "Intentional exclusions" in page and "exclusion list: /signup (all locales)" in page
    r = c.post("/api/exclusions", json={"pattern": "/no-no/konsolidering", "note": "test"},
               headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:a@b.c"})
    assert r.status_code == 200
    queries.clear_cache()
    assert c.get("/api/coverage").json()["total"] == 1
    assert c.post("/api/exclusions/remove", json={"pattern": "/no-no/konsolidering"}).status_code == 200
    assert c.post("/api/exclusions", json={"pattern": "/"}).status_code == 400
    assert {r["type"] for r in s.query("changes")} == {"exclusion"}


def test_one_row_per_url_and_request_button(client):
    from core import rules

    c, s = client
    u = "https://www.corvenia.com/no-no/konsolidering"
    rules.sync_actions(s, [{"rule": "engine_mismatch", "url": u, "detail": "Crawled - currently not indexed", "suggested_fix": "a"},
                           {"rule": "request_indexing", "url": u, "detail": "Priority: new page", "suggested_fix": rules.REQUEST_FIX}])
    queries.clear_cache()
    page = c.get("/coverage").text
    assert page.count('class="req"') == 1 and "engine mismatch" in page and "request indexing" in page
    assert page.count('data-tile=') == 9 and page.count('data-tip=') == 9 and 'id="drill"' in page
    assert 'data-rules="engine_mismatch,request_indexing"' in page and '<td class="act"><button class="req"' in page
    assert c.post("/api/request-indexing", json={"url": u}).status_code == 200
    a = s.get("actions", rules.action_id(u))
    assert a["rules"] == ["engine_mismatch"] and a["status"] == "open" and s.get("url_inventory", u)["gsc_requested_at"]
    assert any(r["type"] == "request_indexing" for r in s.query("changes"))
    assert c.post("/api/request-indexing", json={"url": "https://www.corvenia.com/nope"}).status_code == 404


def test_static_urls_are_versioned(client):
    c, _ = client
    page = c.get("/coverage").text
    assert f"/static/app.css?v={main.STATIC_VERSION}" in page and f"/static/app.js?v={main.STATIC_VERSION}" in page
