"""Read-side views: every 7/28/90-day figure is computed here from raw daily rows.

Rules kept from the spec:
  * One definition and one source per metric; Google, Bing and Clarity are never summed together.
  * Windows end at each source's own latest day (GSC lags ~2 days, Bing aggregates), so the
    current period is "last N days with data" for that source, compared with the N days before.
  * Firestore queries use a single range filter on `day`; everything else is filtered here,
    so no composite indexes are needed.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from core import clusters as cl
from core.store import Store

_cache: dict[tuple, tuple[float, object]] = {}
CACHE_SECONDS = 300


def cached(key: tuple, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def clear_cache() -> None:
    _cache.clear()


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def windows(end: date, days: int) -> tuple[tuple[str, str], tuple[str, str]]:
    cur = (end - timedelta(days=days - 1), end)
    prev = (cur[0] - timedelta(days=days), cur[0] - timedelta(days=1))
    return (cur[0].isoformat(), cur[1].isoformat()), (prev[0].isoformat(), prev[1].isoformat())


def _rows_since(store: Store, coll: str, start: str) -> list[dict]:
    return cached((coll, start), lambda: store.query(coll, [("day", ">=", start)]))


def _latest_day(rows: list[dict]) -> date | None:
    days = [r["day"] for r in rows if r.get("day")]
    return _d(max(days)) if days else None


def _in(r: dict, w: tuple[str, str]) -> bool:
    return w[0] <= r["day"] <= w[1]


def delta(cur: float | None, prev: float | None) -> dict:
    if cur is None:
        return {"value": None, "prev": prev, "pct": None}
    pct = None if not prev else (cur - prev) / prev * 100
    return {"value": cur, "prev": prev, "pct": pct}


# ------------------------------------------------------------------ indexed rate

def active_inventory(store: Store) -> list[dict]:
    return [e for e in cached(("inv",), lambda: store.query("url_inventory")) if not e.get("removed_from_sitemap")]


def indexed_rate_on(store: Store, day: str | None) -> dict:
    """{engine: rate} from the index_status snapshot on `day` (or the nearest earlier snapshot)."""
    if day is None:
        return {}
    since = (_d(day) - timedelta(days=10)).isoformat()
    rows = [r for r in _rows_since(store, "index_status", since) if r["day"] <= day]
    if not rows:
        return {}
    snap_day = max(r["day"] for r in rows)
    out = {}
    for eng in ("google", "bing"):
        rs = [r for r in rows if r["day"] == snap_day and r["engine"] == eng]
        if rs:
            out[eng] = sum(r["status"] == "indexed" for r in rs) / len(rs)
    out["day"] = snap_day
    return out


def indexed_now(store: Store) -> dict:
    inv = active_inventory(store)
    n = len(inv)
    res = {"total": n}
    for eng in ("google", "bing"):
        k = sum((e.get(eng) or {}).get("status") == "indexed" for e in inv)
        res[eng] = {"indexed": k, "rate": (k / n) if n else None}
    return res


# ------------------------------------------------------------------ scorecard

def scorecard(store: Store, days: int = 28) -> dict:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=2 * days + 10)).isoformat()

    gp = _rows_since(store, "gsc_page_daily", start)
    gq = _rows_since(store, "gsc_daily", start)
    bd = [r for r in _rows_since(store, "bing_daily", start) if r.get("kind") == "site"]
    clr = _rows_since(store, "clarity_daily", start)

    tiles = {}
    g_end = _latest_day(gp)
    if g_end:
        cur, prev = windows(g_end, days)
        tiles["google_clicks"] = {**delta(sum(r["clicks"] for r in gp if _in(r, cur)),
                                          sum(r["clicks"] for r in gp if _in(r, prev)) or None),
                                  "window": cur, "source": "Google Search Console (page totals)"}
        nb = [r for r in gq if not r.get("branded")]
        tiles["nonbranded_impressions"] = {**delta(sum(r["impressions"] for r in nb if _in(r, cur)),
                                                   sum(r["impressions"] for r in nb if _in(r, prev)) or None),
                                           "window": cur,
                                           "source": "Google, query rows only (anonymised queries excluded)"}
    b_end = _latest_day(bd)
    if b_end:
        cur, prev = windows(b_end, days)
        tiles["bing_clicks"] = {**delta(sum(r["clicks"] for r in bd if _in(r, cur)),
                                        sum(r["clicks"] for r in bd if _in(r, prev)) or None),
                                "window": cur, "source": "Bing Webmaster (site traffic)"}

    now_rate = indexed_now(store)
    then = indexed_rate_on(store, (today - timedelta(days=days)).isoformat())
    for eng in ("google", "bing"):
        tiles[f"indexed_{eng}"] = {**delta(now_rate[eng]["rate"], then.get(eng)),
                                   "count": now_rate[eng]["indexed"], "total": now_rate["total"],
                                   "prev_day": then.get("day"),
                                   "source": f"Indexing register ({'URL Inspection' if eng == 'google' else 'Bing GetUrlInfo'})"}

    if g_end:  # impression-weighted average position across the priority clusters' queries (Google)
        cur, prev = windows(g_end, days)
        prio = cl.priority_ids()
        pq = [r for r in gq if cl.classify(r.get("query", "")) in prio]
        pc, pp = _wpos([r for r in pq if _in(r, cur)]), _wpos([r for r in pq if _in(r, prev)])
        tiles["priority_position"] = {"value": pc, "prev": pp, "change": None if pc is None or pp is None else pc - pp,
                                      "window": cur, "source": f"Google, priority clusters (list {cl.version()})"}

    ai = [r for r in clr if r.get("ai_referred")]
    c_end = _latest_day(clr)
    if c_end:
        cur, prev = windows(c_end, days)
        tiles["ai_sessions"] = {**delta(sum(r.get("sessions", 0) for r in ai if _in(r, cur)),
                                        sum(r.get("sessions", 0) for r in ai if _in(r, prev)) or None),
                                "window": cur, "source": "Clarity (Source = AI assistant domain)"}
    return {"days": days, "tiles": tiles}


def _wpos(rows: list[dict]) -> float | None:
    imp = sum(r.get("impressions", 0) for r in rows)
    return sum((r.get("position") or 0) * r.get("impressions", 0) for r in rows) / imp if imp else None


def _cluster_table(rows: list[dict], cur: tuple[str, str], prev: tuple[str, str]) -> list[dict]:
    """Per topic cluster: clicks, impressions, impression-weighted position, current vs previous window."""
    by: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {"cur": [], "prev": []})
    for r in rows:
        k = "cur" if _in(r, cur) else "prev" if _in(r, prev) else None
        if k:
            by[cl.classify(r.get("query", ""))][k].append(r)
    out = []
    for c in cl.clusters():
        b = by.get(c["id"], {"cur": [], "prev": []})
        ci, pi = sum(r["impressions"] for r in b["cur"]), sum(r["impressions"] for r in b["prev"])
        if not ci and not pi:
            continue
        pos, ppos = _wpos(b["cur"]), _wpos(b["prev"])
        out.append({**c, "clicks": sum(r["clicks"] for r in b["cur"]), "prev_clicks": sum(r["clicks"] for r in b["prev"]),
                    "impressions": ci, "prev_impressions": pi,
                    "impr_pct": delta(ci, pi or None)["pct"],
                    "position": pos, "prev_position": ppos,
                    "pos_change": None if pos is None or ppos is None else pos - ppos,
                    "queries": len({r["query"] for r in b["cur"]})})
    return out


# ------------------------------------------------------------------ index coverage

STATUSES = ("indexed", "pending", "not_indexed", "blocked", "check_failed", "unknown")


def coverage(store: Store) -> dict:
    """Every active sitemap URL (the register), status counts per engine, and open actions."""
    now = datetime.now(timezone.utc)
    inv = active_inventory(store)
    pages = []
    for e in inv:
        g, b = e.get("google") or {}, e.get("bing") or {}
        sub = b.get("submitted_at") or b.get("first_submitted_at")
        ref = sub or e.get("first_seen")
        pages.append({
            "url": e["url"], "path": e.get("path") or e["url"], "language": e.get("language") or "en",
            "first_seen": e.get("first_seen"), "next_check": e.get("next_check"),
            "google": g.get("status", "unknown"), "google_reason": g.get("detail") or g.get("coverage"),
            "google_last_crawl": g.get("last_crawl"),
            "bing": b.get("status", "unknown"), "bing_reason": b.get("detail"),
            "bing_last_crawl": b.get("last_crawled"),
            "submitted_at": sub,
            "days_since_submit": (now - datetime.fromisoformat(ref.replace("Z", "+00:00"))).days if ref else None,
            "precheck_problem": (e.get("precheck") or {}).get("problem"),
            "gap": not (g.get("status") == "indexed" and b.get("status") == "indexed"),
        })
    pages.sort(key=lambda x: x["path"])
    counts = {eng: {s: sum(p[eng] == s for p in pages) for s in STATUSES} for eng in ("google", "bing")}
    actions = [a for a in store.query("actions") if a.get("status") in ("open", "in_progress")]
    runs = [r for r in store.query("collector_runs", [("collector", "==", "indexing")]) if r.get("started")]
    last = max(runs, key=lambda r: r["started"]) if runs else {}
    return {"total": len(pages), "counts": counts, "pages": pages,
            "gaps": [p for p in pages if p["gap"]], "open_actions": actions,
            "new_last_run": (last.get("counts") or {}).get("new", 0), "last_run": last.get("started")}


# ------------------------------------------------------------------ search performance

def performance(store: Store, days: int = 28) -> dict:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=2 * days + 10)).isoformat()
    gp = _rows_since(store, "gsc_page_daily", start)
    gq = _rows_since(store, "gsc_daily", start)
    bd = _rows_since(store, "bing_daily", start)
    out: dict = {"days": days, "google": None, "bing": None}

    g_end = _latest_day(gp)
    if g_end:
        cur, prev = windows(g_end, days)
        q_cur = [r for r in gq if _in(r, cur)]
        out["google"] = {
            "window": cur, "prev_window": prev,
            "clicks": delta(sum(r["clicks"] for r in gp if _in(r, cur)), sum(r["clicks"] for r in gp if _in(r, prev)) or None),
            "impressions": delta(sum(r["impressions"] for r in gp if _in(r, cur)),
                                 sum(r["impressions"] for r in gp if _in(r, prev)) or None),
            "branded": _split(q_cur, lambda r: "branded" if r.get("branded") else "non-branded"),
            "language": _split([r for r in gp if _in(r, cur)], lambda r: r.get("language") or "en"),
            "queries": _top(q_cur, "query"),
            "pages": _top([r for r in gp if _in(r, cur)], "url"),
            "anonymised_clicks": sum(r["clicks"] for r in gp if _in(r, cur)) - sum(r["clicks"] for r in q_cur),
            "trend": _trend(gp, prev[0], cur[1]),
            "clusters": _cluster_table(gq, cur, prev),
        }
    bs = [r for r in bd if r.get("kind") == "site"]
    b_end = _latest_day(bs) or _latest_day(bd)
    if b_end:
        cur, prev = windows(b_end, days)
        bq = [r for r in bd if r.get("kind") == "query" and _in(r, cur)]
        bp = [r for r in bd if r.get("kind") == "page" and _in(r, cur)]
        out["bing"] = {
            "window": cur, "prev_window": prev,
            "clicks": delta(sum(r["clicks"] for r in bs if _in(r, cur)), sum(r["clicks"] for r in bs if _in(r, prev)) or None),
            "impressions": delta(sum(r["impressions"] for r in bs if _in(r, cur)),
                                 sum(r["impressions"] for r in bs if _in(r, prev)) or None),
            "branded": _split(bq, lambda r: "branded" if r.get("branded") else "non-branded"),
            "language": _split(bp, lambda r: r.get("language") or "en"),
            "queries": _top(bq, "query"),
            "pages": _top(bp, "url"),
            "trend": _trend(bs, prev[0], cur[1]),
            "clusters": _cluster_table([r for r in bd if r.get("kind") == "query"], cur, prev),
        }
    since = out["google"]["prev_window"][0] if out["google"] else (today - timedelta(days=2 * days)).isoformat()
    out["markers"] = sorted({c["day"] for c in store.query("changes", [("day", ">=", since)])
                             if c.get("type") in ("publish", "submit", "fix", "metadata")})
    return out


def _split(rows: list[dict], key) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"clicks": 0, "impressions": 0})
    for r in rows:
        a = agg[key(r)]
        a["clicks"] += r.get("clicks", 0)
        a["impressions"] += r.get("impressions", 0)
    return [{"label": k, **v} for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["impressions"])]


def _top(rows: list[dict], field: str, n: int = 25) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0})
    for r in rows:
        a = agg[r.get(field) or ""]
        a["clicks"] += r.get("clicks", 0)
        a["impressions"] += r.get("impressions", 0)
        a["pos_w"] += (r.get("position") or 0) * r.get("impressions", 0)
    out = []
    for k, a in agg.items():
        imp = a["impressions"]
        out.append({"key": k, "clicks": a["clicks"], "impressions": imp,
                    "ctr": (a["clicks"] / imp) if imp else 0,
                    "position": (a["pos_w"] / imp) if imp else None})
    out.sort(key=lambda x: (-x["clicks"], -x["impressions"]))
    return out[:n]


def _trend(rows: list[dict], start: str, end: str) -> list[tuple[str, int]]:
    by: dict[str, int] = defaultdict(int)
    for r in rows:
        if start <= r["day"] <= end:
            by[r["day"]] += r.get("clicks", 0)
    return sorted(by.items())


# ------------------------------------------------------------------ opportunities

STRIKING_MIN, STRIKING_MAX = 8.0, 20.0
STRIKING_MIN_IMPRESSIONS_PER_28D = 10  # starting threshold (spec: set once data is in, tune after 4 weeks)


def opportunities(store: Store, days: int = 28) -> dict:
    """Striking distance (spec module 4): non-brand queries averaging position 8-20 with enough impressions."""
    today = datetime.now(timezone.utc).date()
    gq = _rows_since(store, "gsc_daily", (today - timedelta(days=2 * days + 10)).isoformat())
    end = _latest_day(gq)
    if not end:
        return {"days": days, "rows": [], "window": None, "min_impressions": None}
    cur, prev = windows(end, days)
    min_imp = max(3, round(STRIKING_MIN_IMPRESSIONS_PER_28D * days / 28))
    agg: dict[str, dict] = {}
    for r in gq:
        k = "cur" if _in(r, cur) else "prev" if _in(r, prev) else None
        if not k:
            continue
        a = agg.setdefault(r["query"], {"cur": [], "prev": []})
        a[k].append(r)
    rows = []
    for q, a in agg.items():
        cid = cl.classify(q)
        if cid in ("brand", "names") or not a["cur"]:
            continue
        imp = sum(r["impressions"] for r in a["cur"])
        pos = _wpos(a["cur"])
        if imp < min_imp or pos is None or not (STRIKING_MIN <= pos <= STRIKING_MAX):
            continue
        pages: dict[str, int] = defaultdict(int)
        for r in a["cur"]:
            pages[r["url"]] += r["impressions"]
        ppos = _wpos(a["prev"])
        clicks = sum(r["clicks"] for r in a["cur"])
        rows.append({"query": q, "cluster": cid, "cluster_label": cl.label(cid), "priority": cid in cl.priority_ids(),
                     "impressions": imp, "clicks": clicks, "ctr": clicks / imp if imp else 0, "position": pos,
                     "prev_position": ppos, "pos_change": None if ppos is None else pos - ppos,
                     "page": max(pages, key=pages.get)})
    rows.sort(key=lambda r: (not r["priority"], -r["impressions"]))
    return {"days": days, "rows": rows, "window": cur, "min_impressions": min_imp,
            "range": (STRIKING_MIN, STRIKING_MAX), "list_version": cl.version()}


# ------------------------------------------------------------------ change log + runs

def change_log(store: Store, kind: str | None = None, limit: int = 300) -> list[dict]:
    rows = store.query("changes", order_by="date", descending=True, limit=limit * 2 if kind else limit)
    if kind:
        rows = [r for r in rows if r.get("type") == kind][:limit]
    return rows


def last_runs(store: Store) -> list[dict]:
    runs = [r for r in store.query("collector_runs") if r.get("kind") != "state" and r.get("started")]
    latest: dict[str, dict] = {}
    for r in runs:
        c = r["collector"]
        if c not in latest or r["started"] > latest[c]["started"]:
            latest[c] = r
    return sorted(latest.values(), key=lambda r: r["collector"])
