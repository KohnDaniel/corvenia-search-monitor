"""MS Clarity Data Export API: daily snapshot (the export only reaches back 1-3 days).

Verified limits (learn.microsoft.com, Clarity Data Export API, checked 2026-11-20):
  GET https://www.clarity.ms/export-data/api/v1/project-live-insights
  Authorization: Bearer <token from Clarity Settings > Data Export>
  numOfDays 1|2|3 (last 24/48/72 h), up to 3 dimensions, 10 requests per project per day,
  max 1 000 rows per response, no pagination, UTC.
  Dimensions: Browser, Device, Country/Region, OS, Source, Medium, Campaign, Channel, URL.

Not documented: the per-metric field names. So every response is stored verbatim in clarity_raw,
and clarity_daily keeps each metric's numeric fields as given (strings coerced to numbers).

This collector uses 2 of the 10 daily requests (URL x Channel, URL x Source), leaving headroom
for a manual re-run. Run it daily, or the data is lost. If a day is missed, run with numOfDays=3
once (python -m collectors clarity --days 3) to recover up to 72 h.
"""

from __future__ import annotations

import urllib.parse
from datetime import timedelta

from collectors.base import OSLO, Run, iso, now_utc
from core import config, http
from core.store import Store
from core.urls import doc_id, language, normalize

ENDPOINT = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
REQUESTS = (("URL", "Channel"), ("URL", "Source"))
DIMENSION_NAMES = {"Browser", "Device", "Country/Region", "OS", "Source", "Medium", "Campaign", "Channel", "URL"}
AI_REFERRERS = ("chatgpt.com", "chat.openai.com", "perplexity.ai", "copilot.microsoft.com",
                "gemini.google.com", "claude.ai", "bing.com/chat")


def _num(v):
    if isinstance(v, (int, float)) or v is None:
        return v
    try:
        f = float(str(v).replace(",", ""))
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def is_ai_source(source: str | None) -> bool:
    s = (source or "").lower()
    return any(a in s for a in AI_REFERRERS)


def collect(store: Store, days: int = 1) -> dict:
    run = Run(store, "clarity")
    token = config.secret("CLARITY_API_TOKEN")
    if not token:
        run.error("CLARITY_API_TOKEN unavailable")
        return run.finish("skipped")
    now = now_utc()
    window_end = now.astimezone(OSLO)
    day = (window_end - timedelta(days=1)).date().isoformat()  # the Oslo day the 24 h window mostly covers

    for dims in REQUESTS:
        params = {"numOfDays": str(days)}
        for i, d in enumerate(dims, 1):
            params[f"dimension{i}"] = d
        code, data = http.get_json(f"{ENDPOINT}?{urllib.parse.urlencode(params)}",
                                   headers={"Authorization": f"Bearer {token}"}, timeout=60)
        store.put("clarity_raw", doc_id(day, days, *dims), {
            "day": day, "fetched_at": iso(now), "num_of_days": days, "dimensions": list(dims),
            "http_status": code, "response": data})
        if code == 429:
            run.error(f"{dims}: daily request limit reached (10/project/day)")
            continue
        if code != 200 or not isinstance(data, list):
            run.error(f"{dims}: HTTP {code} {str(data)[:200]}")
            continue

        rows: dict[str, dict] = {}
        for metric in data:
            name = metric.get("metricName", "unknown")
            for info in metric.get("information", []) or []:
                dimvals = {k: info.get(k) for k in dims}
                url = normalize(dimvals.get("URL") or "")
                second = dims[1]
                key = doc_id(day, days, url, second, dimvals.get(second))
                r = rows.setdefault(key, {
                    "day": day, "window_days": days, "url": url, "language": language(url) if url else None,
                    "dimension": second.lower(), "value": dimvals.get(second),
                    "channel": dimvals.get("Channel"), "source": dimvals.get("Source"),
                    "ai_referred": is_ai_source(dimvals.get("Source")) if second == "Source" else None,
                    "metrics": {}})
                r["metrics"][name] = {k: _num(v) for k, v in info.items() if k not in DIMENSION_NAMES}
        for r in rows.values():  # convenience fields for the app; raw metrics stay alongside
            t = r["metrics"].get("Traffic", {})
            r["sessions"] = _num(t.get("totalSessionCount")) or 0
            r["bot_sessions"] = _num(t.get("totalBotSessionCount")) or 0
        store.put_many("clarity_daily", rows)
        run.count(f"rows_{dims[1].lower()}", len(rows))
        if len(rows) >= 1000:
            run.note(f"{dims}: hit the 1 000-row cap; some pages are missing")
    return run.finish()
