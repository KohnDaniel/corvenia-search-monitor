"""Indexing register, ported from monitor.py (which keeps running in GitHub Actions until retired).

Per run:
  1. Discover URLs from the sitemap.
  2. Pre-check new/changed URLs (HTTP 200, no noindex, self canonical).
  3. Submit passing new/changed URLs to Bing (SubmitUrlBatch). Google: the sitemap is the submission.
  4. Check due URLs: Google URL Inspection + Bing GetUrlInfo, per-URL try/except, per-run caps.
  5. Write url_inventory (register), index_status (URL x engine x day snapshot for every active URL),
     changes (submit events).

Statuses per engine: pending, indexed, not_indexed, blocked, check_failed, unknown.
Never treat "submitted" as "indexed".
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

from collectors.base import OSLO, Run, iso, log, now_utc, parse_iso
from collectors.bing import BingClient
from collectors.gsc import GscClient
from core import config, http
from core.store import Store
from core.urls import doc_id, language, normalize, path_of

CHECK_SCHEDULE_DAYS = [1, 3, 7, 14]
WEEKLY_DAYS = 7
INDEXED_RECHECK_DAYS = 30


def fetch_sitemap_urls(url: str, seen: set | None = None) -> dict[str, str | None]:
    seen = seen if seen is not None else set()
    if url in seen:
        return {}
    seen.add(url)
    status, body, _ = http.get(url)
    if status != 200:
        log(f"sitemap fetch failed {status}: {url}")
        return {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log(f"sitemap parse error {url}: {e}")
        return {}
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out: dict[str, str | None] = {}
    if root.tag.split("}")[-1] == "sitemapindex":
        for sm in root.findall("sm:sitemap", ns):
            loc = sm.findtext("sm:loc", default="", namespaces=ns).strip()
            if loc:
                out.update(fetch_sitemap_urls(loc, seen))
    else:
        for u in root.findall("sm:url", ns):
            loc = u.findtext("sm:loc", default="", namespaces=ns).strip()
            lastmod = u.findtext("sm:lastmod", default=None, namespaces=ns)
            if loc:
                out[loc] = lastmod.strip() if lastmod else None
    return out


def precheck(url: str) -> dict:
    result = {"checked_at": iso(now_utc()), "http_status": None, "noindex": False,
              "canonical": None, "canonical_ok": True, "ok": False, "problem": None}
    status, body, headers = http.get(url, timeout=20)
    result["http_status"] = status
    if status != 200:
        result["problem"] = f"HTTP {status}" if status else f"fetch error: {body[:120].decode(errors='replace')}"
        return result
    xrobots = {k.lower(): v for k, v in headers.items()}.get("x-robots-tag", "")
    html = body[:200_000].decode("utf-8", errors="replace")
    head = html.split("</head>", 1)[0] if "</head>" in html else html
    meta_robots = re.findall(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']+)', head, re.I)
    if "noindex" in xrobots.lower() or any("noindex" in m.lower() for m in meta_robots):
        result.update(noindex=True, problem="noindex")
        return result
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', head, re.I)
    if m:
        canon = m.group(1).strip()
        result["canonical"] = canon
        if normalize(canon) != normalize(url):
            result.update(canonical_ok=False, problem=f"canonical points to {canon}")
            return result
    result["ok"] = True
    return result


def next_check_for(e: dict, now) -> str:
    first = parse_iso(e["first_seen"]) or now
    age_days = (now - first).days
    g, b = e["google"].get("status"), e["bing"].get("status")
    if (g == "indexed" and b in ("indexed", "unknown")) or (b == "indexed" and g == "unknown"):
        return iso(now + timedelta(days=INDEXED_RECHECK_DAYS))
    for d in CHECK_SCHEDULE_DAYS:
        if age_days < d:
            return iso(first + timedelta(days=d))
    return iso(now + timedelta(days=WEEKLY_DAYS))


def new_entry(loc: str, lastmod: str | None, now) -> dict:
    return {"url": normalize(loc), "loc": loc, "path": path_of(loc), "language": language(loc),
            "canonical": None, "cluster": None, "template": None, "first_published": None,
            "first_seen": iso(now), "lastmod": lastmod, "last_change": iso(now), "removed_from_sitemap": None,
            "precheck": None, "google": {"status": "unknown"},
            "bing": {"status": "unknown", "submitted_at": None, "first_submitted_at": None},
            "first_indexed": {"google": None, "bing": None},
            "next_check": iso(now), "history": []}


def _hist(e: dict, now, event: str) -> None:
    e["history"] = (e.get("history", []) + [{"at": iso(now), "event": event}])[-40:]


RESUBMIT_AFTER_DAYS = 7


def due_for_resubmit(register: dict, now, exclude: set[str] = frozenset()) -> list[str]:
    """Active, pre-check-passing pages not crawled by Bing, last submitted RESUBMIT_AFTER_DAYS+ ago.
    Oldest submission first, so a quota cap delays the most recent ones."""
    cutoff = now - timedelta(days=RESUBMIT_AFTER_DAYS)
    out = []
    for k, e in register.items():
        if k in exclude or e.get("removed_from_sitemap") or not (e.get("precheck") or {}).get("ok"):
            continue
        b = e.get("bing") or {}
        if b.get("status") in ("indexed", "blocked"):
            continue
        last = parse_iso(b.get("submitted_at")) or parse_iso(e.get("first_seen"))
        if last and last <= cutoff:
            out.append((last, k))
    return [k for _, k in sorted(out)]


def record_submit(store: Store, url: str, now, actor: str, note: str) -> None:
    store.put("changes", doc_id("submit", url, iso(now)), {
        "date": iso(now), "day": now.astimezone(OSLO).date().isoformat(), "url": normalize(url),
        "type": "submit", "engine": "bing", "note": note, "actor": actor})


def collect(store: Store) -> dict:
    run = Run(store, "indexing")
    now = now_utc()
    register = {d["url"]: d for d in store.query("url_inventory")}

    sitemap = fetch_sitemap_urls(config.SITEMAP_URL)
    run.count("discovered", len(sitemap))
    if not sitemap:
        run.error("sitemap empty or unreachable")
    seen_keys: set[str] = set()
    touched: list[str] = []
    for loc, lastmod in sitemap.items():
        key = normalize(loc)
        seen_keys.add(key)
        e = register.get(key)
        if not e:
            register[key] = new_entry(loc, lastmod, now)
            touched.append(key)
            run.count("new")
            continue
        if e.get("removed_from_sitemap"):
            e["removed_from_sitemap"] = None
            _hist(e, now, "back in sitemap")
        if lastmod and lastmod != e.get("lastmod"):
            e.update(lastmod=lastmod, last_change=iso(now), next_check=iso(now))
            _hist(e, now, "changed in sitemap")
            touched.append(key)
            run.count("changed")
    if sitemap:
        for key, e in register.items():
            if key not in seen_keys and not e.get("removed_from_sitemap"):
                e["removed_from_sitemap"] = iso(now)
                _hist(e, now, "removed from sitemap")
                run.count("removed")

    google, bing = GscClient(), BingClient()

    submit = []
    for key in touched:
        e = register[key]
        try:
            e["precheck"] = precheck(e["loc"])
        except Exception as ex:
            e["precheck"] = {"ok": False, "problem": f"{type(ex).__name__}: {ex}"[:200], "checked_at": iso(now)}
        e["canonical"] = e["precheck"].get("canonical")
        if e["precheck"]["ok"]:
            submit.append(key)
        else:
            _hist(e, now, f"precheck: {e['precheck']['problem']}")
    # Automatic resubmit: pages Bing still has not crawled RESUBMIT_AFTER_DAYS after their last
    # submission go back in the batch, so each page is resubmitted at most once per that interval.
    resubmit = due_for_resubmit(register, now, exclude=set(submit)) if bing.enabled else []
    if bing.enabled and (submit or resubmit):
        room = min(500, bing.daily_quota()) - len(submit)
        if len(resubmit) > max(room, 0):
            run.note(f"Bing quota: resubmitting {max(room, 0)} of {len(resubmit)} due; the rest go tomorrow")
            resubmit = resubmit[:max(room, 0)]
    batch = submit + resubmit
    if batch and bing.enabled:
        res = bing.submit([register[k]["loc"] for k in batch])
        if res["ok"]:
            run.count("bing_submitted", len(submit))
            run.count("bing_resubmitted", len(resubmit))
            for k in batch:
                b = register[k]["bing"]
                b["submitted_at"] = iso(now)
                b["first_submitted_at"] = b.get("first_submitted_at") or iso(now)
                b["submit_count"] = (b.get("submit_count") or 0) + 1
                if k in resubmit:
                    _hist(register[k], now, f"auto-resubmitted to Bing (not crawled {RESUBMIT_AFTER_DAYS}+ days after last submit)")
                    record_submit(store, k, now, "collector:indexing",
                                  f"Automatic resubmit: not crawled by Bing {RESUBMIT_AFTER_DAYS}+ days after last submit")
                else:
                    _hist(register[k], now, "submitted to Bing")
                    record_submit(store, k, now, "collector:indexing", "new or changed in sitemap")
        else:
            run.error(f"Bing submit failed: {res['detail']}")

    due = [e for e in register.values()
           if not e.get("removed_from_sitemap") and (parse_iso(e.get("next_check")) or now) <= now]
    due.sort(key=lambda e: e["first_seen"], reverse=True)
    run.note(f"{len(due)} URLs due for check")
    for i, e in enumerate(due, 1):
        capped = False
        try:
            pc = e.get("precheck")
            if pc is None or (parse_iso(pc.get("checked_at")) or now) < now - timedelta(days=7):
                e["precheck"] = precheck(e["loc"])
                e["canonical"] = e["precheck"].get("canonical")
            if not e["precheck"]["ok"]:
                prob = e["precheck"]["problem"]
                e["google"] = {"status": "blocked", "checked_at": iso(now), "detail": prob}
                e["bing"] = {**e["bing"], "status": "blocked", "checked_at": iso(now), "detail": prob}
            else:
                prev_g, prev_b = e["google"].get("status"), e["bing"].get("status")
                g = google.inspect(e["loc"])
                b = {**e["bing"], **bing.url_info(e["loc"])}
                capped = any("per-run cap" in (x.get("detail") or "") for x in (g, b))

                # A capped or disabled (no credentials) check must not wipe a known status.
                def skipped(res: dict) -> bool:
                    return res["status"] == "unknown" or "per-run cap" in (res.get("detail") or "")

                if skipped(g) and prev_g not in (None, "unknown"):
                    g = e["google"]
                if skipped(b) and prev_b not in (None, "unknown"):
                    b = e["bing"]
                e["google"], e["bing"] = g, b
                for eng, prev, cur in (("google", prev_g, e["google"]["status"]), ("bing", prev_b, b["status"])):
                    if prev != cur:
                        _hist(e, now, f"{eng}: {prev} -> {cur}")
                    if cur == "indexed" and not (e.get("first_indexed") or {}).get(eng):
                        e.setdefault("first_indexed", {})[eng] = iso(now)
        except Exception as ex:  # never let one URL abort the run
            msg = f"{type(ex).__name__}: {ex}"[:200]
            run.error(f"check {e['url']}: {msg}")
            e["google"] = {**e["google"], "status": "check_failed", "checked_at": iso(now), "detail": msg}
            e["bing"] = {**e["bing"], "status": "check_failed", "checked_at": iso(now), "detail": msg}
        e["next_check"] = next_check_for(e, now)
        if capped:  # hit a per-run cap: try again on the next run, not on the slow schedule
            e["next_check"] = iso(now + timedelta(hours=20))
        run.count("checked")
        if i % 25 == 0:
            log(f"  checked {i}/{len(due)} (google {google.inspections}, bing {bing.calls})")
            store.put_many("url_inventory", {k: register[k] for k in (x["url"] for x in due[:i])})
    run.count("google_calls", google.inspections)
    run.count("bing_calls", bing.calls)

    store.put_many("url_inventory", {k: v for k, v in register.items()})

    day = now.astimezone(OSLO).date().isoformat()
    snap = {}
    for key, e in register.items():
        if e.get("removed_from_sitemap"):
            continue
        for eng in ("google", "bing"):
            s = e.get(eng) or {}
            snap[doc_id(key, eng, day)] = {
                "day": day, "url": key, "engine": eng, "status": s.get("status", "unknown"),
                "reason": s.get("detail") or s.get("coverage"),
                "last_crawl": s.get("last_crawl") or s.get("last_crawled"),
                "submitted_at": (e["bing"].get("first_submitted_at") if eng == "bing" else None),
                "checked_at": s.get("checked_at")}
    store.put_many("index_status", snap)
    run.count("index_status_rows", len(snap))
    run.save_state(last_day=day)
    return run.finish()


def import_legacy(store: Store, state_path: str = "data/state.json") -> int:
    """One-off: load the GitHub Actions register (data/state.json) so history and submit dates carry over."""
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    now = now_utc()
    docs = {}
    for loc, old in state.get("urls", {}).items():
        e = new_entry(loc, old.get("lastmod"), now)
        e.update(first_seen=old.get("first_seen"), last_change=old.get("last_change"),
                 removed_from_sitemap=old.get("removed_from_sitemap"), precheck=old.get("precheck"),
                 google=old.get("google") or {"status": "unknown"}, next_check=old.get("next_check"),
                 history=(old.get("history") or [])[-40:])
        b = old.get("bing") or {}
        e["bing"] = {**b, "first_submitted_at": b.get("submitted_at")}
        e["canonical"] = (old.get("precheck") or {}).get("canonical")
        for eng in ("google", "bing"):
            if (e[eng] or {}).get("status") == "indexed":
                e["first_indexed"][eng] = e[eng].get("checked_at")
        docs[e["url"]] = e
        if b.get("submitted_at"):
            store.put("changes", doc_id("submit", e["url"], b["submitted_at"]), {
                "date": b["submitted_at"], "day": b["submitted_at"][:10], "url": e["url"], "type": "submit",
                "engine": "bing", "note": "imported from GitHub Actions register", "actor": "monitor.py"})
    store.put_many("url_inventory", docs)
    log(f"imported {len(docs)} URLs from {state_path}")
    return len(docs)
