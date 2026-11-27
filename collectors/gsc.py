"""Google Search Console: URL Inspection (used by indexing.py) and Search Analytics.

Search Analytics:
  * gsc_daily       dimensions date, page, query, country, device  (query rows; anonymised queries absent)
  * gsc_page_daily  dimensions date, page                          (page totals incl. anonymised queries)
  * First run backfills 16 months; later runs re-fetch from (last stored day - 3) to today,
    because GSC data lags ~2 days and recent days are revised.
  * Dates are stored as GSC gives them (US Pacific days).
"""

from __future__ import annotations

import time
import urllib.parse
from datetime import date, datetime, timedelta

from collectors.base import PACIFIC, Run, iso, log, now_utc
from core import config, google_auth, http
from core.store import Store
from core.urls import doc_id, is_branded, language, normalize

INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
OVERLAP_DAYS = 3
BACKFILL_DAYS = 486  # 16 months
ROW_LIMIT = 25000


class GscClient:
    def __init__(self) -> None:
        self.enabled = False
        self.inspections = 0
        try:
            google_auth.token()
            self.enabled = True
            log(f"GSC: authenticated as {google_auth.identity()}")
        except Exception as e:
            log(f"GSC: auth failed, Google disabled: {type(e).__name__}: {str(e)[:200]}")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {google_auth.token()}"}

    # ------------------------------------------------------------ URL Inspection
    def inspect(self, url: str, cap: int = config.MAX_GOOGLE_INSPECTIONS_PER_RUN) -> dict:
        """~20 s per call, occasional read timeouts: 60 s timeout, one retry, per-run cap."""
        out = {"checked_at": iso(now_utc()), "status": "unknown", "verdict": None, "coverage": None,
               "last_crawl": None, "robots": None, "indexing_state": None, "page_fetch": None,
               "google_canonical": None, "user_canonical": None, "inspected_at": None, "detail": None}
        if not self.enabled:
            return out
        if self.inspections >= cap:
            out.update(status="pending", detail="queued: not yet inspected (per-run cap)")
            return out
        self.inspections += 1
        body = {"inspectionUrl": url, "siteUrl": config.GSC_PROPERTY, "languageCode": "en-US"}
        code, data = http.post_json(INSPECT_URL, body, headers=self._headers(), timeout=60)
        if code == 0 or code >= 500:
            time.sleep(5)
            code, data = http.post_json(INSPECT_URL, body, headers=self._headers(), timeout=60)
        if code != 200:
            out.update(status="check_failed", detail=str(data.get("error", data))[:300])
            return out
        idx = data.get("inspectionResult", {}).get("indexStatusResult", {})
        out.update(verdict=idx.get("verdict"), coverage=idx.get("coverageState"),
                   last_crawl=idx.get("lastCrawlTime"), robots=idx.get("robotsTxtState"),
                   indexing_state=idx.get("indexingState"), page_fetch=idx.get("pageFetchState"),
                   google_canonical=idx.get("googleCanonical"), user_canonical=idx.get("userCanonical"),
                   inspected_at=out["checked_at"])
        cov = (out["coverage"] or "").lower()
        if out["verdict"] == "PASS" and "indexed" in cov and "not indexed" not in cov:
            out["status"] = "indexed"
        elif out["verdict"] in ("FAIL", "NEUTRAL") or "not indexed" in cov or "excluded" in cov:
            out.update(status="not_indexed", detail=out["coverage"])
        else:
            out.update(status="pending", detail=out["coverage"])
        return out

    # ------------------------------------------------------------ Search Analytics
    def query(self, start: date, end: date, dimensions: list[str]) -> list[dict]:
        site = urllib.parse.quote(config.GSC_PROPERTY, safe="")
        url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
        rows: list[dict] = []
        start_row = 0
        while True:
            body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dimensions,
                    "type": "web", "rowLimit": ROW_LIMIT, "startRow": start_row, "dataState": "all"}
            code, data = http.post_json(url, body, headers=self._headers(), timeout=120)
            if code in (0, 429) or code >= 500:
                time.sleep(10)
                code, data = http.post_json(url, body, headers=self._headers(), timeout=120)
            if code != 200:
                raise RuntimeError(f"searchAnalytics {start}..{end} {dimensions}: HTTP {code} {str(data)[:300]}")
            batch = data.get("rows", [])
            for r in batch:
                rows.append({**dict(zip(dimensions, r["keys"])), "clicks": r.get("clicks", 0),
                             "impressions": r.get("impressions", 0), "ctr": r.get("ctr", 0),
                             "position": r.get("position", 0)})
            if len(batch) < ROW_LIMIT:
                return rows
            start_row += ROW_LIMIT


def _chunks(start: date, end: date, days: int):
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=days - 1))
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def collect(store: Store, backfill: bool = False) -> dict:
    run = Run(store, "gsc")
    client = GscClient()
    if not client.enabled:
        run.error("Search Console credentials unavailable")
        return run.finish("skipped")

    today_pt = datetime.now(PACIFIC).date()
    st = run.state()
    last = date.fromisoformat(st["last_day"]) if st.get("last_day") else None
    if backfill or last is None:
        start = today_pt - timedelta(days=BACKFILL_DAYS)
        run.note(f"backfill from {start}")
    else:
        start = last - timedelta(days=OVERLAP_DAYS)
    end = today_pt
    newest = last

    chunk_days = 7 if (end - start).days > 14 else 3
    for a, b in _chunks(start, end, chunk_days):
        try:
            pages = client.query(a, b, ["date", "page"])
            store.put_many("gsc_page_daily", {
                doc_id(r["date"], normalize(r["page"])): {
                    "day": r["date"], "url": normalize(r["page"]), "language": language(r["page"]),
                    "clicks": r["clicks"], "impressions": r["impressions"], "ctr": r["ctr"],
                    "position": r["position"]} for r in pages})
            run.count("page_rows", len(pages))

            rows = client.query(a, b, ["date", "page", "query", "country", "device"])
            store.put_many("gsc_daily", {
                doc_id(r["date"], normalize(r["page"]), r["query"], r["country"], r["device"]): {
                    "day": r["date"], "url": normalize(r["page"]), "query": r["query"],
                    "country": r["country"], "device": r["device"], "language": language(r["page"]),
                    "branded": is_branded(r["query"]), "clicks": r["clicks"],
                    "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"]}
                for r in rows})
            run.count("query_rows", len(rows))
            days_seen = [r["date"] for r in pages]
            if days_seen:
                d = date.fromisoformat(max(days_seen))
                newest = max(newest, d) if newest else d
            log(f"GSC {a}..{b}: {len(pages)} page rows, {len(rows)} query rows")
        except Exception as e:
            run.error(f"{a}..{b}: {type(e).__name__}: {e}")
            break  # keep last_day where it was so the next run retries this window
    if newest:
        run.save_state(last_day=newest.isoformat())
    return run.finish()
