#!/usr/bin/env python3
"""
Corvenia search index monitor.

Daily job:
  1. Discover URLs from the site's sitemap(s).
  2. Pre-check each new/changed URL (HTTP status, noindex, canonical).
  3. Google: inspect index status via Search Console URL Inspection API.
  4. Bing: submit new URLs via Bing Webmaster URL Submission API, read crawl info.
  5. Keep a durable per-URL register with a follow-up schedule.
  6. Write data/status.json and docs/index.html.

Statuses per engine:
  pending            submitted / discovered, no confirmation yet
  indexed            confirmed indexed (Google) or confirmed crawled with HTTP 200 (Bing)
  not_indexed        engine reports not indexed, reason recorded
  blocked            our own pre-check found a blocker (noindex, 4xx/5xx, canonical elsewhere)
  check_failed       API call failed; try again next run
  unknown            no data (e.g. credentials missing)

Never treat "submitted" as "indexed".
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "state.json"
STATUS_FILE = ROOT / "data" / "status.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

SITE_URL = os.environ.get("SITE_URL", "https://www.corvenia.com").rstrip("/")
SITEMAP_URL = os.environ.get("SITEMAP_URL", f"{SITE_URL}/sitemap.xml")
GSC_PROPERTY = os.environ.get("GSC_PROPERTY", "sc-domain:corvenia.com")
GSC_SA_JSON = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
BING_API_KEY = os.environ.get("BING_API_KEY", "")
BING_SITE_URL = os.environ.get("BING_SITE_URL", SITE_URL)

# Follow-up schedule in days after first discovery; weekly after the last entry.
CHECK_SCHEDULE_DAYS = [1, 3, 7, 14]
WEEKLY_DAYS = 7
INDEXED_RECHECK_DAYS = 30
ESCALATE_AFTER_DAYS = 14
MAX_GOOGLE_INSPECTIONS_PER_RUN = int(os.environ.get("MAX_GOOGLE_INSPECTIONS_PER_RUN", "150"))
MAX_BING_CHECKS_PER_RUN = int(os.environ.get("MAX_BING_CHECKS_PER_RUN", "150"))
UA = "CorveniaSearchMonitor/1.0 (+https://www.corvenia.com)"

NOW = datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def log(msg: str) -> None:
    print(f"[{iso(NOW)}] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP helpers

def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw.decode(errors="replace")[:500]}


# ---------------------------------------------------------------- Discovery

def fetch_sitemap_urls(url: str, seen: set | None = None) -> dict[str, str | None]:
    """Return {loc: lastmod} for all URLs, following sitemap indexes."""
    seen = seen or set()
    if url in seen:
        return {}
    seen.add(url)
    status, body, _ = http_get(url)
    if status != 200:
        log(f"sitemap fetch failed {status}: {url}")
        return {}
    out: dict[str, str | None] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log(f"sitemap parse error {url}: {e}")
        return {}
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tag = root.tag.split("}")[-1]
    if tag == "sitemapindex":
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


# ---------------------------------------------------------------- Pre-check

def precheck(url: str) -> dict:
    """Fetch the page and look for blockers."""
    result = {"checked_at": iso(NOW), "http_status": None, "noindex": False,
              "canonical": None, "canonical_ok": True, "ok": False, "problem": None}
    try:
        status, body, headers = http_get(url, timeout=20)
    except Exception as e:
        result["problem"] = f"fetch error: {e}"
        return result
    result["http_status"] = status
    if status != 200:
        result["problem"] = f"HTTP {status}"
        return result
    xrobots = {k.lower(): v for k, v in headers.items()}.get("x-robots-tag", "")
    html = body[:200_000].decode("utf-8", errors="replace")
    head = html.split("</head>", 1)[0] if "</head>" in html else html
    meta_robots = re.findall(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']+)', head, re.I)
    if "noindex" in xrobots.lower() or any("noindex" in m.lower() for m in meta_robots):
        result["noindex"] = True
        result["problem"] = "noindex"
        return result
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', head, re.I)
    if m:
        canon = m.group(1).strip()
        result["canonical"] = canon
        if canon.rstrip("/") != url.rstrip("/"):
            result["canonical_ok"] = False
            result["problem"] = f"canonical points to {canon}"
            return result
    result["ok"] = True
    return result


# ---------------------------------------------------------------- Google

class Google:
    def __init__(self) -> None:
        self.enabled = False
        self.token: str | None = None
        self.calls = 0
        if not GSC_SA_JSON:
            log("Google: GSC_SERVICE_ACCOUNT_JSON not set; Google checks disabled")
            return
        try:
            from google.oauth2 import service_account  # type: ignore
            from google.auth.transport.requests import Request  # type: ignore
            info = json.loads(GSC_SA_JSON)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
            creds.refresh(Request())
            self.token = creds.token
            self.enabled = True
            log(f"Google: authenticated as {info.get('client_email')}")
        except Exception as e:
            log(f"Google: auth failed: {e}")

    def inspect(self, url: str) -> dict:
        """Return a status dict for the URL using the URL Inspection API."""
        out = {"checked_at": iso(NOW), "status": "unknown", "verdict": None,
               "coverage": None, "last_crawl": None, "robots": None, "indexing_state": None,
               "google_canonical": None, "detail": None}
        if not self.enabled:
            return out
        if self.calls >= MAX_GOOGLE_INSPECTIONS_PER_RUN:
            out["status"] = "check_failed"
            out["detail"] = "per-run inspection cap reached"
            return out
        self.calls += 1
        code, data = http_post_json(
            "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
            {"inspectionUrl": url, "siteUrl": GSC_PROPERTY, "languageCode": "en-US"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if code != 200:
            out["status"] = "check_failed"
            out["detail"] = str(data.get("error", data))[:300]
            return out
        idx = data.get("inspectionResult", {}).get("indexStatusResult", {})
        out["verdict"] = idx.get("verdict")
        out["coverage"] = idx.get("coverageState")
        out["last_crawl"] = idx.get("lastCrawlTime")
        out["robots"] = idx.get("robotsTxtState")
        out["indexing_state"] = idx.get("indexingState")
        out["google_canonical"] = idx.get("googleCanonical")
        cov = (out["coverage"] or "").lower()
        if out["verdict"] == "PASS" and "indexed" in cov and "not indexed" not in cov:
            out["status"] = "indexed"
        elif out["verdict"] in ("FAIL", "NEUTRAL") or "not indexed" in cov or "excluded" in cov:
            out["status"] = "not_indexed"
            out["detail"] = out["coverage"]
        else:
            out["status"] = "pending"
            out["detail"] = out["coverage"]
        return out


# ---------------------------------------------------------------- Bing

class Bing:
    BASE = "https://ssl.bing.com/webmaster/api.svc/json"

    def __init__(self) -> None:
        self.enabled = bool(BING_API_KEY)
        self.calls = 0
        if not self.enabled:
            log("Bing: BING_API_KEY not set; Bing checks disabled")

    def submit(self, urls: list[str]) -> dict:
        """Submit up to 500 URLs. Returns {ok, detail}."""
        if not self.enabled or not urls:
            return {"ok": False, "detail": "disabled or empty"}
        code, data = http_post_json(
            f"{self.BASE}/SubmitUrlBatch?apikey={BING_API_KEY}",
            {"siteUrl": BING_SITE_URL, "urlList": urls[:500]},
        )
        ok = code == 200 and "ErrorCode" not in json.dumps(data)
        detail = None if ok else str(data)[:300]
        log(f"Bing: submitted {len(urls[:500])} URLs -> {'ok' if ok else detail}")
        return {"ok": ok, "detail": detail}

    def url_info(self, url: str) -> dict:
        out = {"checked_at": iso(NOW), "status": "unknown", "http_status": None,
               "last_crawled": None, "detail": None}
        if not self.enabled:
            return out
        if self.calls >= MAX_BING_CHECKS_PER_RUN:
            out["status"] = "check_failed"
            out["detail"] = "per-run cap reached"
            return out
        self.calls += 1
        q = urllib.parse.urlencode({"siteUrl": BING_SITE_URL, "url": url, "apikey": BING_API_KEY})
        try:
            code, body, _ = http_get(f"{self.BASE}/GetUrlInfo?{q}")
            data = json.loads(body) if body else {}
        except Exception as e:
            out["status"] = "check_failed"
            out["detail"] = str(e)[:300]
            return out
        if code != 200:
            out["status"] = "check_failed"
            out["detail"] = str(data)[:300]
            return out
        d = data.get("d") or {}
        out["http_status"] = d.get("HttpStatus")
        raw_date = d.get("LastCrawledDate") or ""
        m = re.search(r"/Date\((\d+)", raw_date)
        if m:
            out["last_crawled"] = iso(datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc))
        # Bing exposes crawl facts, not an explicit "indexed" flag.
        # We call it "indexed" only when Bing has crawled it and got HTTP 200.
        if out["last_crawled"] and out["http_status"] == 200:
            out["status"] = "indexed"
            out["detail"] = "crawled with HTTP 200 (Bing does not expose an explicit index flag)"
        elif out["last_crawled"]:
            out["status"] = "not_indexed"
            out["detail"] = f"crawled, HTTP {out['http_status']}"
        else:
            out["status"] = "pending"
            out["detail"] = "not crawled yet"
        return out


# ---------------------------------------------------------------- Scheduling

def next_check_for(entry: dict) -> datetime:
    first = parse_iso(entry["first_seen"]) or NOW
    age_days = (NOW - first).days
    g, b = entry["google"]["status"], entry["bing"]["status"]
    if g == "indexed" and b in ("indexed", "unknown") or (b == "indexed" and g == "unknown"):
        return NOW + timedelta(days=INDEXED_RECHECK_DAYS)
    for d in CHECK_SCHEDULE_DAYS:
        if age_days < d:
            return first + timedelta(days=d)
    return NOW + timedelta(days=WEEKLY_DAYS)


def due(entry: dict) -> bool:
    nc = parse_iso(entry.get("next_check"))
    return nc is None or nc <= NOW


# ---------------------------------------------------------------- Main

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"urls": {}, "runs": []}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


def new_entry(url: str, lastmod: str | None) -> dict:
    return {
        "url": url, "first_seen": iso(NOW), "lastmod": lastmod, "last_change": iso(NOW),
        "removed_from_sitemap": None, "precheck": None,
        "google": {"status": "unknown"}, "bing": {"status": "unknown", "submitted_at": None},
        "next_check": iso(NOW), "history": [],
    }


def main() -> int:
    state = load_state()
    urls_state: dict = state["urls"]
    run = {"started": iso(NOW), "discovered": 0, "new": 0, "changed": 0, "removed": 0,
           "checked": 0, "google_calls": 0, "bing_calls": 0, "bing_submitted": 0, "errors": []}

    # 1. Discover
    sitemap = fetch_sitemap_urls(SITEMAP_URL)
    run["discovered"] = len(sitemap)
    if not sitemap:
        run["errors"].append("sitemap empty or unreachable")
    new_urls: list[str] = []
    for url, lastmod in sitemap.items():
        e = urls_state.get(url)
        if not e:
            urls_state[url] = new_entry(url, lastmod)
            new_urls.append(url)
            run["new"] += 1
        else:
            if e.get("removed_from_sitemap"):
                e["removed_from_sitemap"] = None
            if lastmod and lastmod != e.get("lastmod"):
                e["lastmod"] = lastmod
                e["last_change"] = iso(NOW)
                e["next_check"] = iso(NOW)
                e["history"].append({"at": iso(NOW), "event": "changed in sitemap"})
                new_urls.append(url)  # re-submit changed pages to Bing
                run["changed"] += 1
    if sitemap:
        for url, e in urls_state.items():
            if url not in sitemap and not e.get("removed_from_sitemap"):
                e["removed_from_sitemap"] = iso(NOW)
                e["history"].append({"at": iso(NOW), "event": "removed from sitemap"})
                run["removed"] += 1

    google = Google()
    bing = Bing()

    # 2. Pre-check + Bing submit for new/changed URLs that pass
    submit: list[str] = []
    for url in new_urls:
        e = urls_state[url]
        e["precheck"] = precheck(url)
        if e["precheck"]["ok"]:
            submit.append(url)
        else:
            e["history"].append({"at": iso(NOW), "event": f"precheck: {e['precheck']['problem']}"})
    if submit:
        res = bing.submit(submit)
        if res["ok"]:
            run["bing_submitted"] = len(submit)
            for url in submit:
                urls_state[url]["bing"]["submitted_at"] = iso(NOW)
                urls_state[url]["history"].append({"at": iso(NOW), "event": "submitted to Bing"})
        elif bing.enabled:
            run["errors"].append(f"Bing submit failed: {res['detail']}")

    # 3. Check everything that is due
    due_entries = [e for e in urls_state.values() if due(e) and not e.get("removed_from_sitemap")]
    due_entries.sort(key=lambda e: e["first_seen"], reverse=True)  # newest first
    for e in due_entries:
        url = e["url"]
        if e.get("precheck") is None or (parse_iso(e["precheck"]["checked_at"]) < NOW - timedelta(days=7)):
            e["precheck"] = precheck(url)
        if not e["precheck"]["ok"]:
            e["google"] = {"status": "blocked", "checked_at": iso(NOW), "detail": e["precheck"]["problem"]}
            e["bing"] = {**e["bing"], "status": "blocked", "checked_at": iso(NOW), "detail": e["precheck"]["problem"]}
        else:
            prev_g, prev_b = e["google"].get("status"), e["bing"].get("status")
            g = google.inspect(url)
            b = {**e["bing"], **bing.url_info(url)}
            e["google"], e["bing"] = g, b
            for name, prev, cur in (("google", prev_g, g["status"]), ("bing", prev_b, b["status"])):
                if prev != cur:
                    e["history"].append({"at": iso(NOW), "event": f"{name}: {prev} -> {cur}"})
        e["next_check"] = iso(next_check_for(e))
        run["checked"] += 1
    run["google_calls"], run["bing_calls"] = google.calls, bing.calls

    # 4. Escalations: still not indexed after ESCALATE_AFTER_DAYS
    alerts = []
    for e in urls_state.values():
        if e.get("removed_from_sitemap"):
            continue
        age = (NOW - (parse_iso(e["first_seen"]) or NOW)).days
        if e["precheck"] and not e["precheck"]["ok"]:
            alerts.append({"url": e["url"], "type": "blocked", "detail": e["precheck"]["problem"]})
        elif age >= ESCALATE_AFTER_DAYS and e["google"]["status"] in ("pending", "not_indexed"):
            alerts.append({"url": e["url"], "type": "google_not_indexed", "days": age,
                           "detail": e["google"].get("detail")})
        if age >= ESCALATE_AFTER_DAYS and e["bing"]["status"] in ("pending", "not_indexed"):
            alerts.append({"url": e["url"], "type": "bing_not_crawled", "days": age,
                           "detail": e["bing"].get("detail")})

    run["finished"] = iso(datetime.now(timezone.utc))
    state["runs"] = (state.get("runs", []) + [run])[-60:]
    save_json(STATE_FILE, state)

    # 5. Public status file + dashboard
    summary = {"google": {}, "bing": {}}
    active = [e for e in urls_state.values() if not e.get("removed_from_sitemap")]
    for e in active:
        for eng in ("google", "bing"):
            s = e[eng]["status"]
            summary[eng][s] = summary[eng].get(s, 0) + 1
    status = {
        "generated_at": run["finished"], "site": SITE_URL, "total_urls": len(active),
        "summary": summary, "alerts": alerts, "last_run": run,
        "integrations": {"google": google.enabled, "bing": bing.enabled},
        "urls": [{
            "url": e["url"], "first_seen": e["first_seen"], "last_change": e["last_change"],
            "next_check": e["next_check"],
            "precheck": {"ok": e["precheck"]["ok"], "problem": e["precheck"]["problem"]} if e["precheck"] else None,
            "google": e["google"], "bing": e["bing"],
        } for e in sorted(active, key=lambda x: x["first_seen"], reverse=True)],
    }
    save_json(STATUS_FILE, status)
    write_dashboard(status)
    log(f"done: {len(active)} URLs, new {run['new']}, checked {run['checked']}, "
        f"alerts {len(alerts)}, errors {run['errors']}")
    return 0


# ---------------------------------------------------------------- Dashboard

def write_dashboard(status: dict) -> None:
    template = (ROOT / "docs" / "template.html").read_text() if (ROOT / "docs" / "template.html").exists() else None
    if not template:
        return
    html = template.replace("/*__STATUS_JSON__*/null", json.dumps(status, ensure_ascii=False))
    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(html)


if __name__ == "__main__":
    sys.exit(main())
