"""Intentional exclusions: pages that are meant to stay out of the index.

Excluded URLs are not counted as problems (no actions) and are left out of the indexed-rate
denominator. Three sources:
  * the exclusion list (settings/exclusions), editable from the Index coverage page;
  * automatic: the pre-check found noindex;
  * automatic: the URL was removed from the sitemap (already outside the active register).

List entries: {"pattern", "all_locales", "note", "added_by", "added"}.
  * A pattern without "*" matches that path and everything under it (/email-assets matches
    /email-assets/x). A pattern with "*" is a glob over the whole path.
  * all_locales: the pattern also matches the /no-no/ and /sv-se/ copies.
"""

from __future__ import annotations

import fnmatch
import urllib.parse
from datetime import datetime, timezone

from core.store import Store
from core.urls import LOCALE_PREFIXES, path_of

COLLECTION, DOC = "settings", "exclusions"

SEED = [
    *({"pattern": p, "all_locales": True, "note": "Form/utility page"} for p in
      ("/signup", "/signup-failed", "/signup-success", "/email-assets")),
    *({"pattern": f"/{loc}{p}", "all_locales": False, "note": "Untranslated copy of the English page"}
      for p in ("/terms-of-use", "/built-for-trust", "/press-releases", "/group-intelligence-assessment")
      for loc in ("no-no", "sv-se")),
]


def clean_pattern(p: str) -> str:
    p = (p or "").strip()
    if "://" in p:
        p = urllib.parse.urlsplit(p).path  # not normalize(): that would percent-encode "*"
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def _strip_locale(path: str) -> str:
    first, _, rest = path.strip("/").partition("/")
    return "/" + rest if first.lower() in LOCALE_PREFIXES else path


def _match(path: str, pattern: str) -> bool:
    path, pattern = path.lower(), pattern.lower()
    if "*" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def matching_entry(url: str, entries: list[dict]) -> dict | None:
    path = path_of(url)
    bare = _strip_locale(path)
    for x in entries:
        pat = x["pattern"]
        if _match(path, pat) or (x.get("all_locales") and _match(bare, _strip_locale(pat))):
            return x
    return None


def reason(e: dict, entries: list[dict]) -> str | None:
    """Why this register entry is excluded, or None if it counts."""
    if e.get("removed_from_sitemap"):
        return "removed from sitemap"
    if (e.get("precheck") or {}).get("noindex"):
        return "noindex (pre-check)"
    x = matching_entry(e.get("url") or "", entries)
    return f"exclusion list: {x['pattern']}{' (all locales)' if x.get('all_locales') else ''}" if x else None


def load(store: Store) -> list[dict]:
    """The exclusion list; seeded on first use (an emptied list stays empty)."""
    doc = store.get(COLLECTION, DOC)
    if doc is None:
        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        doc = {"entries": [{**x, "added_by": "seed (index review 2026-11-27)", "added": stamp} for x in SEED]}
        store.put(COLLECTION, DOC, doc, merge=False)
    return list(doc.get("entries") or [])


def add(store: Store, pattern: str, all_locales: bool, note: str, actor: str) -> dict:
    entries = load(store)
    pat = clean_pattern(pattern)
    entries = [x for x in entries if x["pattern"].lower() != pat.lower()]
    entry = {"pattern": pat, "all_locales": bool(all_locales), "note": (note or "").strip()[:200],
             "added_by": actor, "added": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    store.put(COLLECTION, DOC, {"entries": entries + [entry]}, merge=False)
    return entry


def remove(store: Store, pattern: str) -> bool:
    entries = load(store)
    pat = clean_pattern(pattern)
    keep = [x for x in entries if x["pattern"].lower() != pat.lower()]
    store.put(COLLECTION, DOC, {"entries": keep}, merge=False)
    return len(keep) != len(entries)
