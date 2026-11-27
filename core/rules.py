"""Alert rules -> action queue (spec: "Cross-source insights and alert rules").

evaluate_index_rules() returns one alert per (rule, url). sync_actions() merges them into ONE action
row per URL with a list of rule tags (actions/<doc_id("url", url)>). "opened" is the date the first
rule fired for that URL and is not reset when another rule starts firing on the same URL; per-rule
first-fire dates are kept in rule_opened. A row stays open until no rule fires for the URL; actions
are never deleted. Thresholds are starting values.

Google's reason (URL Inspection coverageState) drives both the detail text and the suggested fix.
Intentionally excluded URLs (core/exclusions.py) never raise alerts.

Google has no submission API, so for priority URLs the rules add a manual "request_indexing" tag:
request indexing in Search Console (URL Inspection -> Request indexing, quota about 10 a day), then
mark it requested in the app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import exclusions as excl
from core.store import Store
from core.urls import doc_id, normalize

ENGINE_MISMATCH_DAYS = 7
SLOW_PUBLISH_HOURS = 72
NOT_INDEXED_DAYS = 14
GSC_REQUESTS_PER_DAY = 10
GSC_REREQUEST_DAYS = 14
FIX_LOOKBACK_DAYS = 30

RULE_ORDER = ("blocked", "engine_mismatch", "slow_publish", "google_not_indexed", "bing_not_indexed",
              "request_indexing")

REQUEST_FIX = ("Request indexing in GSC: URL Inspection → Request indexing (manual, about 10 a day), "
               "then press Requested")

# (substring of coverageState, lower case) -> suggested fix. First match wins.
COVERAGE_FIXES = (
    ("noindex", "Intended? Add it to the exclusion list to hide it. Otherwise remove the noindex tag"),
    ("google chose different canonical", "Translate the page properly, or point the canonical to the URL "
                                         "Google chose, or noindex the copy"),
    ("without user-selected canonical", "Add a canonical; check for a copy in another locale or collection"),
    ("crawled - currently not indexed", "Content quality/uniqueness: expand the page, link it from the hub and "
                                        "related pages, then request indexing manually in GSC"),
    ("discovered - currently not indexed", "Add internal links from crawled pages; check that the URL is in the sitemap"),
    ("unknown to google", "Check that the URL is in the sitemap; add internal links"),
    ("redirect", "Remove from the register (take it out of the sitemap, or add it to the exclusion list), "
                 "or fix the link"),
    ("not found", "Remove from the register (take it out of the sitemap, or add it to the exclusion list), "
                  "or fix the link"),
    ("404", "Remove from the register (take it out of the sitemap, or add it to the exclusion list), or fix the link"),
    ("robots.txt", "Unblock the URL in robots.txt, or add it to the exclusion list if intended"),
)
DEFAULT_GOOGLE_FIX = "Check the coverage reason in URL Inspection; add internal links; check canonical"
# Requesting indexing helps only for these; the others need a fix first.
REQUESTABLE = ("crawled - currently not indexed", "discovered - currently not indexed", "unknown to google")


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _cov(g: dict) -> str:
    """coverageState in lower case with GSC's typographic quotes and dashes flattened."""
    return ((g.get("coverage") or "").lower()
            .replace("‘", "'").replace("’", "'").replace("–", "-").replace("—", "-"))


def google_fix(g: dict) -> str:
    cov = _cov(g)
    return next((fix for key, fix in COVERAGE_FIXES if key in cov), DEFAULT_GOOGLE_FIX)


def google_reason(g: dict, url: str) -> str:
    """Detail text: Google's coverageState, plus the canonical Google chose when it is another URL."""
    parts = [g.get("coverage") or g.get("detail") or f"Google: {g.get('status') or 'unknown'}"]
    gc = g.get("google_canonical")
    if gc and normalize(gc) != normalize(url):
        parts.append(f"Google chose: {gc}")
    return " · ".join(parts)


def evaluate_index_rules(inventory: list[dict], now: datetime | None = None,
                         exclusions: list[dict] = (), changes: list[dict] = ()) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    fixes: dict[str, datetime] = {}  # url -> latest logged fix/metadata/publish
    for c in changes:
        if c.get("type") in ("fix", "metadata", "publish") and (d := _dt(c.get("date"))):
            u = normalize(c.get("url") or "")
            fixes[u] = max(fixes.get(u, d), d)
    out: list[dict] = []
    request: list[tuple] = []
    requested_today = 0
    for e in inventory:
        if (_dt(e.get("gsc_requested_at")) or datetime.min.replace(tzinfo=timezone.utc)) > now - timedelta(hours=24):
            requested_today += 1
        if excl.reason(e, list(exclusions)):
            continue
        url = e["url"]
        gd = e.get("google") or {}
        g = gd.get("status")
        b = (e.get("bing") or {}).get("status")
        pc = e.get("precheck") or {}
        first = _dt(e.get("first_seen")) or now
        submitted = _dt((e.get("bing") or {}).get("first_submitted_at")) or first
        g_detail, g_fix = google_reason(gd, url), google_fix(gd)

        if pc and pc.get("ok") is False:
            out.append({"rule": "blocked", "url": url, "detail": pc.get("problem"),
                        "suggested_fix": "Fix immediately: restore HTTP 200 / self canonical"})
            continue
        one_engine_only = (g == "indexed") != (b == "indexed") and "unknown" not in (g, b)
        if one_engine_only and now - submitted >= timedelta(days=ENGINE_MISMATCH_DAYS):
            if g == "indexed":
                out.append({"rule": "engine_mismatch", "url": url, "detail": "not indexed in Bing",
                            "suggested_fix": "Resubmitted to Bing automatically every 7 days; check canonical and robots"})
            else:
                out.append({"rule": "engine_mismatch", "url": url, "detail": g_detail, "suggested_fix": g_fix})
        published = _dt(e.get("first_published")) or first
        slow = (g != "indexed" and now - published >= timedelta(hours=SLOW_PUBLISH_HOURS)
                and now - published < timedelta(days=NOT_INDEXED_DAYS))
        if slow:
            out.append({"rule": "slow_publish", "url": url, "detail": g_detail, "suggested_fix": g_fix})
        if now - first >= timedelta(days=NOT_INDEXED_DAYS):
            if g in ("pending", "not_indexed"):
                out.append({"rule": "google_not_indexed", "url": url, "detail": g_detail, "suggested_fix": g_fix})
            if b in ("pending", "not_indexed"):
                out.append({"rule": "bing_not_indexed", "url": url, "detail": (e.get("bing") or {}).get("detail"),
                            "suggested_fix": "Being resubmitted to Bing automatically; if it persists, check content "
                                             "quality and internal links"})

        # Manual GSC request queue: priority = a new page slow to index, or a page with a fix logged
        # since its last request. Only where requesting can help (not noindex/duplicate/redirect/404).
        if g not in ("pending", "not_indexed"):
            continue
        cov = _cov(gd)
        if cov and not any(k in cov for k in REQUESTABLE):
            continue
        last_req = _dt(e.get("gsc_requested_at"))
        if last_req and now - last_req < timedelta(days=GSC_REREQUEST_DAYS):
            continue
        fixed = fixes.get(normalize(url))
        fixed = fixed if fixed and now - fixed <= timedelta(days=FIX_LOOKBACK_DAYS) and (not last_req or fixed > last_req) else None
        if fixed or slow:
            why = f"Priority: fix logged {fixed.date().isoformat()}" if fixed else "Priority: new page not indexed after 72 h"
            # Fixed pages first (most recent fix first), then the newest pages.
            request.append(((0, -fixed.timestamp()) if fixed else (1, -first.timestamp()), url, why))
    room = max(GSC_REQUESTS_PER_DAY - requested_today, 0)
    for _, url, why in sorted(request)[:room]:
        out.append({"rule": "request_indexing", "url": url, "detail": why, "suggested_fix": REQUEST_FIX})
    return out


def _uniq(xs) -> list:
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def merge_by_url(alerts: list[dict]) -> dict[str, dict]:
    by: dict[str, list[dict]] = {}
    for a in alerts:
        by.setdefault(a["url"], []).append(a)
    out = {}
    for url, xs in by.items():
        xs.sort(key=lambda a: RULE_ORDER.index(a["rule"]) if a["rule"] in RULE_ORDER else 99)
        out[url] = {"url": url, "rules": _uniq(a["rule"] for a in xs),
                    "details": _uniq(a.get("detail") for a in xs),
                    "suggested_fixes": _uniq(a.get("suggested_fix") for a in xs)}
    return out


def action_id(url: str) -> str:
    return doc_id("url", url)


def sync_actions(store: Store, alerts: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    existing = {a["_id"]: a for a in store.query("actions")}
    # Rows from before the one-row-per-URL change: one per (rule, url). Their open dates carry over.
    legacy: dict[str, list[dict]] = {}
    for a in existing.values():
        if "rules" not in a and a.get("url") and a.get("status") in ("open", "in_progress"):
            legacy.setdefault(a["url"], []).append(a)

    merged = merge_by_url(alerts)
    live, handled = set(), set()
    opened = 0
    for url, m in merged.items():
        aid = action_id(url)
        live.add(aid)
        cur = existing.get(aid)
        old = legacy.get(url, [])
        old_first = {a["rule"]: a["opened"] for a in old if a.get("rule") and a.get("opened")}
        fields = {"url": url, "rules": m["rules"], "details": m["details"], "suggested_fixes": m["suggested_fixes"],
                  "detail": " · ".join(m["details"]), "suggested_fix": " | ".join(m["suggested_fixes"]),
                  "last_seen": stamp}
        if cur and cur.get("status") in ("open", "in_progress"):
            ro = dict(cur.get("rule_opened") or {})
            for r in m["rules"]:
                ro.setdefault(r, old_first.get(r, stamp))
            store.put("actions", aid, {**fields, "rule_opened": ro})
        elif not cur or cur.get("closed_by") != "user":
            ro = {r: old_first.get(r, stamp) for r in m["rules"]}
            first = min([stamp, *old_first.values()])
            store.put("actions", aid, {**fields, "status": "open", "owner": (cur or {}).get("owner"),
                                       "rule_opened": ro, "opened": first, "closed": None}, merge=False)
            opened += 1
        for a in old:
            store.put("actions", a["_id"], {"status": "closed", "closed": stamp, "closed_by": "merged"})
            handled.add(a["_id"])
    closed = 0
    for aid, a in existing.items():
        if aid in live or aid in handled:
            continue
        if a.get("status") in ("open", "in_progress") and a.get("closed_by") != "user":
            store.put("actions", aid, {"status": "closed", "closed": stamp, "closed_by": "rules"})
            closed += 1
    return {"opened": opened, "closed": closed, "live": len(live), "alerts": len(alerts)}


def mark_gsc_requested(store: Store, url: str, now: datetime | None = None) -> bool:
    """The user requested indexing in GSC: stamp the register and drop the tag from the URL's row."""
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    url = normalize(url)
    found = store.query("url_inventory", [("url", "==", url)], limit=1)  # by field: older local copies use other ids
    if not found:
        return False
    store.put("url_inventory", found[0].get("_id") or url, {"gsc_requested_at": stamp})
    a = store.get("actions", action_id(url))
    if a and "request_indexing" in (a.get("rules") or []):
        keep = [r for r in a["rules"] if r != "request_indexing"]
        upd = {"rules": keep}
        # details/suggested_fixes are deduplicated lists, not parallel to rules; drop the request texts.
        upd["suggested_fixes"] = [f for f in a.get("suggested_fixes") or [] if f != REQUEST_FIX]
        upd["details"] = [d for d in a.get("details") or [] if not str(d).startswith("Priority: ")]
        upd["detail"] = " · ".join(upd["details"])
        upd["suggested_fix"] = " | ".join(upd["suggested_fixes"])
        if not keep:
            upd.update(status="closed", closed=stamp, closed_by="rules")
        store.put("actions", action_id(url), upd)
    return True
