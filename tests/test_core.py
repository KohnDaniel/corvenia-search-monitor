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


def test_auto_resubmit_after_7_days(tmp_path, monkeypatch):
    from collectors import indexing

    s = JsonStore(tmp_path)
    now = datetime.now(timezone.utc)
    ok_pc = {"ok": True, "checked_at": now.isoformat()}
    later = (now + timedelta(days=30)).isoformat()  # not due for a status check in this run

    def page(path, bing, submitted_days_ago):
        u = f"https://www.corvenia.com/{path}"
        sub = (now - timedelta(days=submitted_days_ago)).isoformat()
        s.put("url_inventory", u, {**indexing.new_entry(u, None, now), "first_seen": sub, "precheck": ok_pc,
                                   "google": {"status": "indexed"}, "next_check": later,
                                   "bing": {"status": bing, "submitted_at": sub, "first_submitted_at": sub}})
        return u

    stale = page("stale", "pending", 8)          # not crawled, last submit 8 days ago -> resubmit
    fresh = page("fresh", "pending", 3)          # submitted 3 days ago -> wait
    done = page("done", "indexed", 20)           # crawled -> never resubmit
    monkeypatch.setattr(indexing, "fetch_sitemap_urls", lambda url: {stale: None, fresh: None, done: None})

    sent = []

    class FakeBing:
        enabled, calls = True, 0

        def daily_quota(self):
            return 100

        def submit(self, urls):
            sent.extend(urls)
            return {"ok": True, "detail": None, "count": len(urls)}

    class NoGoogle:
        enabled, inspections = False, 0

    monkeypatch.setattr(indexing, "BingClient", FakeBing)
    monkeypatch.setattr(indexing, "GscClient", NoGoogle)
    res = indexing.collect(s)
    assert sent == [stale]
    assert res["counts"]["bing_resubmitted"] == 1
    assert s.get("url_inventory", stale)["bing"]["submitted_at"] > (now - timedelta(minutes=1)).isoformat()
    rows = s.query("changes")
    assert len(rows) == 1 and rows[0]["actor"] == "collector:indexing" and "Automatic resubmit" in rows[0]["note"]
    sent.clear()
    indexing.collect(s)
    assert sent == []  # resubmitted at most once per 7 days


def test_rules_open_and_close(tmp_path):
    s = JsonStore(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    inv = [{"url": "https://www.corvenia.com/a", "first_seen": old, "precheck": {"ok": True},
            "google": {"status": "indexed"}, "bing": {"status": "pending", "first_submitted_at": old}}]
    alerts = rules.evaluate_index_rules(inv)
    assert {a["rule"] for a in alerts} == {"engine_mismatch", "bing_not_indexed"}
    assert rules.sync_actions(s, alerts)["opened"] == 1  # one row per URL
    rows = s.query("actions")
    assert len(rows) == 1 and rows[0]["rules"] == ["engine_mismatch", "bing_not_indexed"]
    assert rules.sync_actions(s, alerts)["opened"] == 0  # idempotent
    assert rules.sync_actions(s, [])["closed"] == 1


G = "https://www.corvenia.com"


def test_reason_keyed_fix_and_detail():
    g = {"status": "not_indexed", "coverage": "Duplicate, Google chose different canonical than user",
         "google_canonical": f"{G}/konsolidering"}
    assert rules.google_reason(g, f"{G}/no-no/konsolidering") ==         "Duplicate, Google chose different canonical than user · Google chose: https://www.corvenia.com/konsolidering"
    assert rules.google_fix(g).startswith("Translate the page properly")
    assert rules.google_reason({**g, "google_canonical": f"{G}/no-no/konsolidering/"}, f"{G}/no-no/konsolidering") == g["coverage"]
    cases = {"Excluded by ‘noindex’ tag": "Intended?", "Duplicate without user-selected canonical": "Add a canonical",
             "Crawled - currently not indexed": "Content quality", "Discovered – currently not indexed": "Add internal links",
             "URL is unknown to Google": "Check that the URL is in the sitemap", "Page with redirect": "Remove from the register",
             "Not found (404)": "Remove from the register", "Something new": "Check the coverage reason"}
    for cov, start in cases.items():
        assert rules.google_fix({"coverage": cov}).startswith(start), cov
    # The Detail column carries coverageState instead of "not indexed in Google".
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    inv = [{"url": f"{G}/x", "first_seen": old, "precheck": {"ok": True},
            "google": {"status": "not_indexed", "coverage": "Crawled - currently not indexed"},
            "bing": {"status": "indexed", "first_submitted_at": old}}]
    a = next(a for a in rules.evaluate_index_rules(inv) if a["rule"] == "engine_mismatch")
    assert a["detail"] == "Crawled - currently not indexed" and a["suggested_fix"].startswith("Content quality")


def test_exclusions_seed_and_matching(tmp_path):
    from core import exclusions as ex

    s = JsonStore(tmp_path)
    entries = ex.load(s)
    assert len(entries) == 12 and s.get("settings", "exclusions")
    hit = lambda path: ex.matching_entry(G + path, entries) is not None
    assert hit("/signup") and hit("/no-no/signup") and hit("/sv-se/signup-success") and hit("/email-assets/welcome")
    assert hit("/no-no/terms-of-use") and hit("/sv-se/press-releases") and hit("/sv-se/group-intelligence-assessment")
    assert not hit("/terms-of-use") and not hit("/press-releases") and not hit("/signups") and not hit("/no-no/pricing")
    ex.add(s, "https://www.corvenia.com/blog/draft-*/", False, "drafts", "t")
    assert ex.matching_entry(f"{G}/blog/draft-1", ex.load(s)) and not ex.matching_entry(f"{G}/no-no/blog/draft-1", ex.load(s))
    assert ex.remove(s, "/blog/draft-*") and len(ex.load(s)) == 12
    for e in ex.load(s):
        ex.remove(s, e["pattern"])
    assert ex.load(s) == []  # an emptied list is not re-seeded
    assert ex.reason({"url": f"{G}/a", "precheck": {"ok": False, "noindex": True}}, []) == "noindex (pre-check)"
    assert ex.reason({"url": f"{G}/a", "removed_from_sitemap": "2026-11-01"}, []) == "removed from sitemap"


def test_excluded_urls_raise_no_alerts():
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    from core import exclusions as ex

    base = {"first_seen": old, "google": {"status": "not_indexed", "coverage": "Excluded by 'noindex' tag"},
            "bing": {"status": "indexed", "first_submitted_at": old}}
    inv = [{**base, "url": f"{G}/no-no/signup", "precheck": {"ok": True}},
           {**base, "url": f"{G}/landing", "precheck": {"ok": False, "noindex": True, "problem": "noindex"}},
           {**base, "url": f"{G}/real", "precheck": {"ok": True}}]
    alerts = rules.evaluate_index_rules(inv, exclusions=ex.SEED)
    assert {a["url"] for a in alerts} == {f"{G}/real"}


def test_opened_kept_when_rule_added_and_legacy_rows_merged(tmp_path):
    s = JsonStore(tmp_path)
    u = f"{G}/a"
    day1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    # Legacy per-rule row from before the merge.
    s.put("actions", "legacy", {"rule": "engine_mismatch", "url": u, "status": "open", "opened": "2026-10-20T06:00:00+00:00"})
    rules.sync_actions(s, [{"rule": "engine_mismatch", "url": u, "detail": "d", "suggested_fix": "f"}], now=day1)
    rules.sync_actions(s, [{"rule": "engine_mismatch", "url": u, "detail": "d", "suggested_fix": "f"},
                           {"rule": "slow_publish", "url": u, "detail": "d", "suggested_fix": "f"}],
                       now=day1 + timedelta(days=2))
    a = s.get("actions", rules.action_id(u))
    assert a["opened"] == "2026-10-20T06:00:00+00:00" and a["rules"] == ["engine_mismatch", "slow_publish"]
    assert a["rule_opened"]["slow_publish"].startswith("2026-11-03") and a["details"] == ["d"]
    assert s.get("actions", "legacy")["closed_by"] == "merged"
    assert sum(r.get("status") == "open" for r in s.query("actions")) == 1


def test_request_indexing_queue(tmp_path):
    s = JsonStore(tmp_path)
    now = datetime.now(timezone.utc)
    new = (now - timedelta(days=4)).isoformat()
    inv = [{"url": f"{G}/p{i}", "first_seen": new, "precheck": {"ok": True},
            "google": {"status": "not_indexed", "coverage": "Discovered - currently not indexed"},
            "bing": {"status": "indexed", "first_submitted_at": new}} for i in range(14)]
    inv.append({**inv[0], "url": f"{G}/dup", "google": {"status": "not_indexed", "coverage": "Duplicate without user-selected canonical"}})
    old = (now - timedelta(days=60)).isoformat()
    inv.append({**inv[0], "url": f"{G}/fixed", "first_seen": old})
    changes = [{"type": "fix", "url": f"{G}/fixed", "date": (now - timedelta(days=1)).isoformat()}]
    req = [a for a in rules.evaluate_index_rules(inv, changes=changes) if a["rule"] == "request_indexing"]
    assert len(req) == 10 and req[0]["url"] == f"{G}/fixed" and f"{G}/dup" not in {a["url"] for a in req}
    for e in inv:
        s.put("url_inventory", e["url"], e)
    rules.sync_actions(s, rules.evaluate_index_rules(inv, changes=changes))
    assert rules.mark_gsc_requested(s, f"{G}/fixed")
    a = s.get("actions", rules.action_id(f"{G}/fixed"))
    assert a["rules"] == ["google_not_indexed"] and a["status"] == "open"  # the other rule keeps the row open
    assert rules.mark_gsc_requested(s, f"{G}/p0")
    assert s.get("actions", rules.action_id(f"{G}/p0"))["rules"] == ["slow_publish"]
    inv = s.query("url_inventory")
    req = [a for a in rules.evaluate_index_rules(inv, changes=changes) if a["rule"] == "request_indexing"]
    assert len(req) == 8 and f"{G}/fixed" not in {a["url"] for a in req}  # 2 used today; not re-asked for 14 days
