"""Webflow Data API v2: content inventory and publish events; also the action layer for the app.

Reads (token scopes: sites:read, pages:read, cms:read, forms:read):
  * site lastPublished          -> changes row type=publish when it moves
  * pages per locale            -> webflow_pages, and title/seo/first_published onto url_inventory
  * CMS items lastPublished     -> changes rows type=publish per item URL
  * form submissions per day    -> counted in collector_runs only for now (conversion event still open)
Writes (used by the app later; scopes pages:write, sites:write): update_page_seo(), publish_site().
"""

from __future__ import annotations

from collectors.base import Run, iso, now_utc
from core import config, http
from core.store import Store
from core.urls import doc_id, normalize

API = "https://api.webflow.com/v2"


class WebflowClient:
    def __init__(self) -> None:
        self.token = config.secret("WEBFLOW_API_TOKEN")
        self.site = config.WEBFLOW_SITE_ID
        self.enabled = bool(self.token)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "accept": "application/json"}

    def get(self, path: str, **params) -> dict:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        code, data = http.get_json(f"{API}{path}{'?' + qs if qs else ''}", headers=self._h(), timeout=60)
        if code != 200:
            raise RuntimeError(f"Webflow GET {path}: HTTP {code} {str(data)[:300]}")
        return data

    def paged(self, path: str, key: str, **params) -> list[dict]:
        out, offset = [], 0
        while True:
            data = self.get(path, limit=100, offset=offset, **params)
            items = data.get(key, [])
            out.extend(items)
            total = (data.get("pagination") or {}).get("total", len(out))
            offset += len(items)
            if not items or offset >= total:
                return out

    def update_page_seo(self, page_id: str, locale_id: str | None, title: str | None, description: str | None) -> dict:
        seo = {k: v for k, v in (("title", title), ("description", description)) if v is not None}
        qs = f"?localeId={locale_id}" if locale_id else ""
        code, data = http.send_json(f"{API}/pages/{page_id}{qs}", {"seo": seo}, headers=self._h(), method="PUT")
        if code != 200:
            raise RuntimeError(f"Webflow update page {page_id}: HTTP {code} {str(data)[:300]}")
        return data

    def publish_site(self, domain_ids: list[str]) -> dict:
        code, data = http.post_json(f"{API}/sites/{self.site}/publish", {"customDomains": domain_ids},
                                    headers=self._h())
        if code not in (200, 202):
            raise RuntimeError(f"Webflow publish: HTTP {code} {str(data)[:300]}")
        return data


def _locale_prefix(locales: dict, locale_id: str | None) -> str:
    loc = locales.get(locale_id or "")
    if not loc or loc.get("primary"):
        return ""
    sub = loc.get("subdirectory") or loc.get("tag") or ""
    return f"/{sub.strip('/')}" if sub else ""


def collect(store: Store) -> dict:
    run = Run(store, "webflow")
    wf = WebflowClient()
    if not wf.enabled:
        run.error("WEBFLOW_API_TOKEN unavailable")
        return run.finish("skipped")
    now = now_utc()

    try:
        site = wf.get(f"/sites/{wf.site}")
    except Exception as e:
        run.error(str(e))
        return run.finish("failed")
    st = run.state()
    last_pub = site.get("lastPublished")
    if last_pub and last_pub != st.get("site_last_published"):
        store.put("changes", doc_id("publish", "site", last_pub), {
            "date": last_pub, "day": last_pub[:10], "url": normalize(config.SITE_URL + "/"), "type": "publish",
            "note": "Webflow site published", "actor": "webflow"})
        run.count("site_publish_events")

    locales: dict = {}
    loc_block = site.get("locales") or {}
    for loc in [loc_block.get("primary")] + list(loc_block.get("secondary") or []):
        if loc:
            locales[loc.get("id") or loc.get("cmsLocaleId") or ""] = {**loc, "primary": loc is loc_block.get("primary")}

    pages_docs, inv_updates = {}, {}
    for locale_id in list(locales) or [None]:
        # The API rejects localeId for the primary locale ("must be a secondary locale"): omit it there.
        query_locale = None if (locales.get(locale_id or "") or {}).get("primary") else locale_id
        try:
            pages = wf.paged(f"/sites/{wf.site}/pages", "pages", localeId=query_locale)
        except Exception as e:
            run.error(f"pages {locale_id}: {e}")
            continue
        prefix = _locale_prefix(locales, locale_id)
        for p in pages:
            if p.get("draft") or p.get("archived") or p.get("collectionId"):
                continue  # drafts, archived and CMS template pages have no single public URL
            path = p.get("publishedPath") or ("/" + (p.get("slug") or "")).rstrip("/")
            if prefix and not path.startswith(prefix):
                path = prefix + (path if path != "/" else "")
            url = normalize(path or "/")
            pages_docs[doc_id(p["id"], locale_id)] = {
                "page_id": p["id"], "locale_id": locale_id, "url": url, "title": p.get("title"),
                "seo_title": (p.get("seo") or {}).get("title"), "seo_description": (p.get("seo") or {}).get("description"),
                "created_on": p.get("createdOn"), "last_updated": p.get("lastUpdated"), "synced_at": iso(now)}
            inv_updates[url] = {"webflow_page_id": p["id"], "webflow_locale_id": locale_id, "title": p.get("title"),
                                "seo_title": (p.get("seo") or {}).get("title"),
                                "seo_description": (p.get("seo") or {}).get("description"),
                                "template": "page", "first_published": p.get("createdOn")}
    store.put_many("webflow_pages", pages_docs)
    run.count("pages", len(pages_docs))

    try:
        for c in wf.get(f"/sites/{wf.site}/collections").get("collections", []):
            slug = c.get("slug")
            for it in wf.paged(f"/collections/{c['id']}/items/live", "items"):
                fd = it.get("fieldData") or {}
                url = normalize(f"/{slug}/{fd.get('slug', '')}")
                inv_updates.setdefault(url, {}).update(template=f"cms:{slug}", title=fd.get("name"),
                                                       first_published=it.get("createdOn"))
                if it.get("lastPublished"):
                    store.put("changes", doc_id("publish", url, it["lastPublished"]), {
                        "date": it["lastPublished"], "day": it["lastPublished"][:10], "url": url,
                        "type": "publish", "note": f"CMS item published ({c.get('displayName') or slug})",
                        "actor": "webflow"})
                run.count("cms_items")
    except Exception as e:
        run.error(f"cms: {e}")

    # Only enrich URLs the sitemap already knows; never invent inventory rows.
    known = {d["url"] for d in store.query("url_inventory")}
    store.put_many("url_inventory", {u: v for u, v in inv_updates.items() if u in known})
    run.count("inventory_enriched", len([u for u in inv_updates if u in known]))

    try:
        forms = wf.paged(f"/sites/{wf.site}/forms", "forms")
        run.count("forms", len(forms))
    except Exception as e:
        run.note(f"forms not read: {e}")

    run.save_state(site_last_published=last_pub)
    return run.finish()
