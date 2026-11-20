"""Bing Webmaster API: URL submission + GetUrlInfo (used by indexing.py and the app) and traffic stats.

Hard-won rules:
  * GetUrlInfo throttles bursts ("ErrorCode 5 ThrottleHost") after ~20 rapid calls:
    pace >= 1.5 s, back off 10/20/30 s on throttle.
  * HttpStatus 0 = healthy / not reported; only >= 400 is an error.
  * No explicit "indexed" flag: a crawl date is the positive signal.
  * SubmitUrlBatch (<= 500 URLs) works with siteUrl=https://corvenia.com (domain-level site).

Stats written to bing_daily, dates as Bing gives them (Bing aggregates; rows can be weekly buckets):
  kind=site   GetRankAndTrafficStats  (day totals: clicks, impressions)
  kind=query  GetQueryStats           (query, clicks, impressions, avg positions)
  kind=page   GetPageStats            (page URL, clicks, impressions, avg positions)
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone

from collectors.base import Run, iso, log, now_utc
from core import config, http
from core.store import Store
from core.urls import doc_id, is_branded, language, normalize

BASE = "https://ssl.bing.com/webmaster/api.svc/json"


def bing_date(raw: str | None) -> str | None:
    """'/Date(1316156400000-0700)/' -> '2011-09-16' (UTC day)."""
    m = re.search(r"/Date\((-?\d+)", raw or "")
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()


def bing_datetime(raw: str | None) -> str | None:
    m = re.search(r"/Date\((-?\d+)", raw or "")
    return iso(datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)) if m else None


class BingClient:
    def __init__(self) -> None:
        self.key = config.secret("BING_API_KEY")
        self.enabled = bool(self.key)
        self.calls = 0
        if not self.enabled:
            log("Bing: BING_API_KEY unavailable; Bing disabled")

    def _get(self, method: str, **params) -> tuple[int, dict]:
        q = urllib.parse.urlencode({"siteUrl": config.BING_SITE_URL, **params, "apikey": self.key})
        data: dict = {}
        code = 0
        for attempt in range(4):
            time.sleep(config.BING_PAUSE_SECONDS)
            code, data = http.get_json(f"{BASE}/{method}?{q}", timeout=60)
            text = json.dumps(data)
            throttled = code == 429 or "Throttle" in text
            if code == 200 and not throttled and "ErrorCode" not in text:
                return code, data
            if throttled and attempt < 3:
                time.sleep(10 * (attempt + 1))  # 10 s, 20 s, 30 s
                continue
            return code, data
        return code, data

    def submit(self, urls: list[str]) -> dict:
        if not self.enabled or not urls:
            return {"ok": False, "detail": "Bing disabled" if not self.enabled else "no URLs"}
        q = urllib.parse.urlencode({"apikey": self.key})
        code, data = http.post_json(f"{BASE}/SubmitUrlBatch?{q}",
                                    {"siteUrl": config.BING_SITE_URL, "urlList": urls[:500]})
        ok = code == 200 and "ErrorCode" not in json.dumps(data)
        detail = None if ok else str(data)[:300]
        log(f"Bing: submitted {len(urls[:500])} URLs -> {'ok' if ok else detail}")
        return {"ok": ok, "detail": detail, "count": len(urls[:500])}

    def url_info(self, url: str, cap: int = config.MAX_BING_CHECKS_PER_RUN) -> dict:
        out = {"checked_at": iso(now_utc()), "status": "unknown", "http_status": None,
               "last_crawled": None, "detail": None}
        if not self.enabled:
            return out
        if self.calls >= cap:
            out.update(status="pending", detail="queued: not yet checked (per-run cap)")
            return out
        self.calls += 1
        code, data = self._get("GetUrlInfo", url=url)
        if code != 200 or "ErrorCode" in json.dumps(data):
            out.update(status="check_failed", detail=str(data)[:300])
            return out
        d = data.get("d") or {}
        http_status = d.get("HttpStatus") or 0
        out["http_status"] = http_status or None
        out["last_crawled"] = bing_datetime(d.get("LastCrawledDate"))
        if out["last_crawled"] and http_status < 400:
            out.update(status="indexed",
                       detail=f"crawled by Bing {out['last_crawled'][:10]} (no explicit index flag in Bing API)")
        elif out["last_crawled"]:
            out.update(status="not_indexed", detail=f"Bing crawl got HTTP {http_status}")
        else:
            out.update(status="pending", detail="not crawled by Bing yet")
        return out

    def stats(self, method: str) -> list[dict]:
        code, data = self._get(method)
        if code != 200:
            raise RuntimeError(f"{method}: HTTP {code} {str(data)[:300]}")
        return data.get("d") or []


def collect(store: Store) -> dict:
    run = Run(store, "bing")
    client = BingClient()
    if not client.enabled:
        run.error("BING_API_KEY unavailable")
        return run.finish("skipped")

    jobs = (("site", "GetRankAndTrafficStats"), ("query", "GetQueryStats"), ("page", "GetPageStats"))
    for kind, method in jobs:
        try:
            rows = client.stats(method)
        except Exception as e:
            run.error(str(e))
            continue
        docs = {}
        for r in rows:
            day = bing_date(r.get("Date"))
            if not day:
                continue
            key = "" if kind == "site" else (r.get("Query") or "")
            doc = {"day": day, "kind": kind, "clicks": r.get("Clicks", 0) or 0,
                   "impressions": r.get("Impressions", 0) or 0,
                   "click_position": r.get("AvgClickPosition"), "position": r.get("AvgImpressionPosition")}
            if kind == "query":
                doc.update(query=key, branded=is_branded(key))
            elif kind == "page":
                doc.update(url=normalize(key), language=language(key))
            docs[doc_id(kind, day, normalize(key) if kind == "page" else key)] = doc
        store.put_many("bing_daily", docs)
        run.count(f"{kind}_rows", len(docs))
    return run.finish()
