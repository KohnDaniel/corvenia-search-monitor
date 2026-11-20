from datetime import datetime, timedelta, timezone

from collectors.bing import bing_date
from core import rules
from core.store import JsonStore
from core.urls import is_branded, language, normalize


def test_normalize_join_key():
    assert normalize("https://corvenia.com/no-no/konsolidering/?utm=x#top") == "https://www.corvenia.com/no-no/konsolidering"
    assert normalize("HTTPS://WWW.Corvenia.com/") == "https://www.corvenia.com/"
    assert normalize("/sv-se/") == "https://www.corvenia.com/sv-se"
    assert normalize("www.corvenia.com/pricing") == "https://www.corvenia.com/pricing"
    assert normalize("https://www.corvenia.com") == "https://www.corvenia.com/"


def test_language_and_brand():
    assert language("https://www.corvenia.com/no-no/x") == "no"
    assert language("https://www.corvenia.com/sv-se") == "sv"
    assert language("https://www.corvenia.com/pricing") == "en"
    assert is_branded("Corvenia konsolidering") and is_branded("corveina") and not is_branded("konsernregnskap")


def test_bing_date():
    assert bing_date("/Date(1316156400000-0700)/") == "2011-09-16"
    assert bing_date(None) is None


def test_json_store_upsert_and_query(tmp_path):
    s = JsonStore(tmp_path)
    s.put("gsc_daily", "a", {"day": "2026-11-01", "clicks": 1})
    s.put("gsc_daily", "a", {"impressions": 5})  # merge
    s.put_many("gsc_daily", {"b": {"day": "2026-11-03", "clicks": 2}})
    assert s.get("gsc_daily", "a") == {"_id": "a", "day": "2026-11-01", "clicks": 1, "impressions": 5}
    assert [r["_id"] for r in s.query("gsc_daily", [("day", ">=", "2026-11-02")])] == ["b"]
    assert JsonStore(tmp_path).get("gsc_daily", "b")["clicks"] == 2  # persisted to disk
    s.put("url_inventory", "https://www.corvenia.com/x", {"url": "https://www.corvenia.com/x"})
    d = s.get("url_inventory", "https://www.corvenia.com/x")
    assert d["url"] == "https://www.corvenia.com/x" and "/" not in d["_id"]  # Firestore-safe id


def test_indexing_keeps_known_status_when_engine_disabled(tmp_path, monkeypatch):
    from collectors import indexing

    s = JsonStore(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    u = "https://www.corvenia.com/a"
    s.put("url_inventory", u, {**indexing.new_entry(u, None, datetime.now(timezone.utc)), "first_seen": old,
                               "precheck": {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat()},
                               "google": {"status": "indexed"}, "bing": {"status": "indexed"}, "next_check": old})
    monkeypatch.setattr(indexing, "fetch_sitemap_urls", lambda url: {u: None})

    class Off:
        enabled, inspections, calls = False, 0, 0

        def inspect(self, url):
            return {"status": "unknown"}

        def url_info(self, url):
            return {"status": "unknown"}

    monkeypatch.setattr(indexing, "GscClient", Off)
    monkeypatch.setattr(indexing, "BingClient", Off)
    indexing.collect(s)
    e = s.get("url_inventory", u)
    assert e["google"]["status"] == "indexed" and e["bing"]["status"] == "indexed"


def test_rules_open_and_close(tmp_path):
    s = JsonStore(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    inv = [{"url": "https://www.corvenia.com/a", "first_seen": old, "precheck": {"ok": True},
            "google": {"status": "indexed"}, "bing": {"status": "pending", "first_submitted_at": old}}]
    alerts = rules.evaluate_index_rules(inv)
    assert {a["rule"] for a in alerts} == {"engine_mismatch", "bing_not_indexed"}
    assert rules.sync_actions(s, alerts)["opened"] == 2
    assert rules.sync_actions(s, alerts)["opened"] == 0  # idempotent
    assert rules.sync_actions(s, [])["closed"] == 2
