"""Corvenia Marketing Control — web app (Cloud Run, behind Identity-Aware Proxy).

Login is IAP's job: the service accepts traffic only from IAP (no unauthenticated invoker), and IAP
adds X-Goog-Authenticated-User-Email. There is no auth code here; the header is used to attribute
actions (changes rows). Locally the user is "local-dev".

  uvicorn app.main:app --reload
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import charts, queries
from collectors.base import OSLO, iso
from core import exclusions, rules
from core.store import get_store
from core.urls import doc_id, normalize

HERE = Path(__file__).parent
app = FastAPI(title="Corvenia Marketing Control", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# Static URLs carry a hash of the files, so a deploy never runs new HTML against a cached old CSS/JS.
STATIC_VERSION = hashlib.sha1(b"".join((HERE / "static" / f).read_bytes() for f in ("app.css", "app.js"))).hexdigest()[:10]
templates.env.globals.update(charts=charts, static_version=STATIC_VERSION)
templates.env.filters["num"] = lambda v: "—" if v is None else f"{v:,.0f}".replace(",", " ")
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.1f} %"
templates.env.filters["delta"] = lambda v: "" if v is None else f"{'+' if v >= 0 else ''}{v:.0f} %"
templates.env.filters["pos"] = lambda v: "—" if v is None else f"{v:.1f}"
templates.env.filters["day"] = lambda v: (v or "")[:10] or "—"
# Position change: lower is better, so a negative change is an improvement.
templates.env.filters["posdelta"] = lambda v: "" if v is None else f"{'▲' if v < 0 else '▼' if v > 0 else '='} {abs(v):.1f}"

PERIODS = (7, 28, 90)


def user_of(request: Request) -> str:
    raw = request.headers.get("x-goog-authenticated-user-email", "")
    return raw.split(":", 1)[-1] if raw else os.environ.get("LOCAL_USER", "local-dev")


def period(request: Request) -> int:
    try:
        d = int(request.query_params.get("days", 28))
    except ValueError:
        d = 28
    return d if d in PERIODS else 28


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    store = get_store()
    ctx.setdefault("days", period(request))
    return templates.TemplateResponse(request, name, {
        "user": user_of(request), "periods": PERIODS, "path": request.url.path,
        "runs": queries.cached(("runs",), lambda: queries.last_runs(store)), **ctx})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def scorecard_page(request: Request):
    days = period(request)
    return render(request, "scorecard.html", sc=queries.scorecard(get_store(), days))


@app.get("/coverage", response_class=HTMLResponse)
def coverage_page(request: Request):
    return render(request, "coverage.html", cov=queries.coverage(get_store()))


@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request):
    days = period(request)
    return render(request, "performance.html", perf=queries.performance(get_store(), days))


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_page(request: Request):
    days = period(request)
    return render(request, "opportunities.html", opp=queries.opportunities(get_store(), days))


@app.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request):
    kind = request.query_params.get("type") or None
    return render(request, "changes.html", rows=queries.change_log(get_store(), kind), kind=kind)


# ------------------------------------------------------------------ actions

def _log_change(store, url: str, kind: str, note: str, actor: str, **extra) -> dict:
    now = datetime.now(timezone.utc)
    row = {"date": iso(now), "day": now.astimezone(OSLO).date().isoformat(), "url": url if kind == "exclusion" else normalize(url),  # a pattern, not a page
           "type": kind, "note": note, "actor": actor, **extra}
    store.put("changes", doc_id(kind, row["url"], row["date"], actor), row)
    queries.clear_cache()
    return row


# Bing submission is fully automatic (collectors/indexing.py): new/changed pages on discovery, and pages
# not crawled 7 days after their last submit are resubmitted. There is deliberately no manual button.


@app.post("/api/changes")
async def log_fix(request: Request):
    body = await request.json()
    url, note = body.get("url", "").strip(), body.get("note", "").strip()
    kind = body.get("type", "fix")
    if not url or not note or kind not in ("fix", "metadata", "publish"):
        raise HTTPException(400, "url, note and a valid type are required")
    return {"ok": True, "change": _log_change(get_store(), url, kind, note[:500], user_of(request))}


@app.post("/api/exclusions")
async def add_exclusion(request: Request):
    body = await request.json()
    pattern = (body.get("pattern") or "").strip()
    if not pattern or pattern == "/":
        raise HTTPException(400, "a path or pattern is required (not the home page)")
    store = get_store()
    entry = exclusions.add(store, pattern, bool(body.get("all_locales")), body.get("note", ""), user_of(request))
    _log_change(store, entry["pattern"], "exclusion", f"Added to exclusion list{' (all locales)' if entry['all_locales'] else ''}"
                f"{': ' + entry['note'] if entry['note'] else ''}", user_of(request))
    return {"ok": True, "entry": entry}


@app.post("/api/exclusions/remove")
async def remove_exclusion(request: Request):
    body = await request.json()
    store = get_store()
    if not exclusions.remove(store, body.get("pattern", "")):
        raise HTTPException(404, "not on the exclusion list")
    _log_change(store, exclusions.clean_pattern(body["pattern"]), "exclusion", "Removed from exclusion list", user_of(request))
    return {"ok": True}


@app.post("/api/request-indexing")
async def request_indexing(request: Request):
    """Records that someone requested indexing in Search Console (there is no API for it)."""
    body = await request.json()
    url = normalize(body.get("url", ""))
    store = get_store()
    if not rules.mark_gsc_requested(store, url):
        raise HTTPException(404, "URL not in the register")
    return {"ok": True, "change": _log_change(store, url, "request_indexing",
                                              "Requested indexing in Google Search Console (URL Inspection)", user_of(request))}


@app.get("/api/scorecard")
def api_scorecard(request: Request):
    return queries.scorecard(get_store(), period(request))


@app.get("/api/coverage")
def api_coverage():
    return queries.coverage(get_store())
