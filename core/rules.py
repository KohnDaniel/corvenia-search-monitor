"""Alert rules -> action queue (spec: "Cross-source insights and alert rules").

Session 1 implements the index rules, whose inputs exist now. Each rule opens one action per
(rule, url) and leaves it open until a later evaluation no longer triggers it; actions are never
deleted (closed ones stay visible for the change log). Thresholds are starting values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.store import Store
from core.urls import doc_id

ENGINE_MISMATCH_DAYS = 7
SLOW_PUBLISH_HOURS = 72
NOT_INDEXED_DAYS = 14


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evaluate_index_rules(inventory: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for e in inventory:
        if e.get("removed_from_sitemap"):
            continue
        url = e["url"]
        g = (e.get("google") or {}).get("status")
        b = (e.get("bing") or {}).get("status")
        pc = e.get("precheck") or {}
        first = _dt(e.get("first_seen")) or now
        submitted = _dt((e.get("bing") or {}).get("first_submitted_at")) or first

        if pc and pc.get("ok") is False:
            out.append({"rule": "blocked", "url": url, "detail": pc.get("problem"),
                        "suggested_fix": "Fix immediately: remove noindex / restore HTTP 200 / self canonical"})
            continue
        one_engine_only = (g == "indexed") != (b == "indexed") and "unknown" not in (g, b)
        if one_engine_only and now - submitted >= timedelta(days=ENGINE_MISMATCH_DAYS):
            missing = "Bing" if g == "indexed" else "Google"
            out.append({"rule": "engine_mismatch", "url": url, "detail": f"not indexed in {missing}",
                        "suggested_fix": "Resubmitted to Bing automatically every 7 days; check canonical and robots" if missing == "Bing"
                        else "Inspect URL in Search Console; add internal links; check canonical"})
        published = _dt(e.get("first_published")) or first
        if g != "indexed" and now - published >= timedelta(hours=SLOW_PUBLISH_HOURS) and now - published < timedelta(days=NOT_INDEXED_DAYS):
            out.append({"rule": "slow_publish", "url": url, "detail": f"Google: {g}",
                        "suggested_fix": "Inspect URL in Search Console, add internal links"})
        if now - first >= timedelta(days=NOT_INDEXED_DAYS):
            for eng, s in (("google", g), ("bing", b)):
                if s in ("pending", "not_indexed"):
                    out.append({"rule": f"{eng}_not_indexed", "url": url,
                                "detail": (e.get(eng) or {}).get("detail"),
                                "suggested_fix": "Being resubmitted to Bing automatically; if it persists, check content quality and internal links"
                                if eng == "bing" else "Check coverage reason in URL Inspection"})
    return out


def sync_actions(store: Store, alerts: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    existing = {a["_id"]: a for a in store.query("actions")}
    live = set()
    opened = 0
    for a in alerts:
        aid = doc_id(a["rule"], a["url"])
        live.add(aid)
        cur = existing.get(aid)
        if cur and cur.get("status") in ("open", "in_progress"):
            store.put("actions", aid, {"detail": a.get("detail"), "suggested_fix": a.get("suggested_fix"), "last_seen": stamp})
        elif not cur or cur.get("status") == "closed" and cur.get("closed_by") == "rules":
            store.put("actions", aid, {**a, "status": "open", "owner": (cur or {}).get("owner"),
                                       "opened": stamp, "closed": None, "last_seen": stamp}, merge=False)
            opened += 1
    closed = 0
    for aid, a in existing.items():
        if aid not in live and a.get("status") in ("open", "in_progress") and a.get("closed_by") != "user":
            store.put("actions", aid, {"status": "closed", "closed": stamp, "closed_by": "rules"})
            closed += 1
    return {"opened": opened, "closed": closed, "live": len(live)}
