"""Corvenia Marketing Control — web app (Cloud Run, behind Identity-Aware Proxy).

Login is IAP's job: the service accepts traffic only from IAP (no unauthenticated invoker), and IAP
adds X-Goog-Authenticated-User-Email. There is no auth code here; the header is used to attribute
actions (changes rows). Locally the user is "local-dev".

  uvicorn app.main:app --reload
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import charts, queries
from collectors.base import OSLO, iso
from collectors.bing import BingClient
from core.store import get_store
from core.urls import doc_id, normalize

HERE = Path(__file__).parent
app = FastAPI(title="Corvenia Marketing Control", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(charts=charts)
templates.env.filters["num"] = lambda v: "—" if v is None else f"{v:,.0f}".replace(",", " ")
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.1f} %"
templates.env.filters["delta"] = lambda v: "" if v is None else f"{'+' if v >= 0 else ''}{v:.0f} %"
templates.env.filters["pos"] = lambda v: "—" if v is None else f"{v:.1f}"
templates.env.filters["day"] = lambda v: (v or "")[:10] or "—"

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


@app.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request):
    kind = request.query_params.get("type") or None
    return render(request, "changes.html", rows=queries.change_log(get_store(), kind), kind=kind)


# ------------------------------------------------------------------ actions

def _log_change(store, url: str, kind: str, note: str, actor: str, **extra) -> dict:
    now = datetime.now(timezone.utc)
    row = {"date": iso(now), "day": now.astimezone(OSLO).date().isoformat(), "url": normalize(url),
           "type": kind, "note": note, "actor": actor, **extra}
    store.put("changes", doc_id(kind, row["url"], row["date"], actor), row)
    queries.clear_cache()
    return row


@app.post("/api/resubmit")
async def resubmit(request: Request):
    body = await request.json()
    url = normalize(body.get("url", ""))
    store = get_store()
    entry = store.get("url_inventory", url)
    if not entry:
        raise HTTPException(404, "URL is not in the sitemap inventory")
    bing = BingClient()
    if not bing.enabled:
        raise HTTPException(503, "BING_API_KEY is not available to the app")
    res = bing.submit([entry.get("loc") or url])
    actor = user_of(request)
    if not res["ok"]:
        _log_change(store, url, "submit", f"Resubmit to Bing FAILED: {res['detail']}", actor, engine="bing", ok=False)
        return JSONResponse({"ok": False, "detail": res["detail"]}, status_code=502)
    now = iso(datetime.now(timezone.utc))
    b = {**(entry.get("bing") or {}), "submitted_at": now}
    b["first_submitted_at"] = b.get("first_submitted_at") or now
    hist = (entry.get("history") or []) + [{"at": now, "event": f"resubmitted to Bing by {actor}"}]
    store.put("url_inventory", url, {"bing": b, "history": hist[-40:], "next_check": now})
    row = _log_change(store, url, "submit", "Resubmitted to Bing from the app", actor, engine="bing", ok=True)
    return {"ok": True, "change": row}


@app.post("/api/changes")
async def log_fix(request: Request):
    body = await request.json()
    url, note = body.get("url", "").strip(), body.get("note", "").strip()
    kind = body.get("type", "fix")
    if not url or not note or kind not in ("fix", "metadata", "publish"):
        raise HTTPException(400, "url, note and a valid type are required")
    return {"ok": True, "change": _log_change(get_store(), url, kind, note[:500], user_of(request))}


@app.get("/api/scorecard")
def api_scorecard(request: Request):
    return queries.scorecard(get_store(), period(request))


@app.get("/api/coverage")
def api_coverage():
    return queries.coverage(get_store())
