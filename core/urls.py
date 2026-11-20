"""URL normalisation: the join key across every source.

Rule (spec, build notes): lowercase scheme and host, drop query string and fragment,
one trailing-slash rule (none, except the root "/"), and a single host (www.corvenia.com),
so GSC, Bing, Clarity, Webflow and the sitemap all meet on the same string.
"""

from __future__ import annotations

import hashlib
import urllib.parse

CANONICAL_HOST = "www.corvenia.com"
_HOST_ALIASES = {"corvenia.com", "www.corvenia.com"}

LOCALE_PREFIXES = {"no-no": "no", "sv-se": "sv"}


def normalize(url: str) -> str:
    """Return the join key for a URL or path. Paths are resolved against the canonical host."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("/"):
        url = f"https://{CANONICAL_HOST}{url}"
    elif "://" not in url:
        url = f"https://{url}"
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if host in _HOST_ALIASES:
        host = CANONICAL_HOST
    path = urllib.parse.unquote(p.path or "/")
    path = urllib.parse.quote(path, safe="/-._~%")
    if len(path) > 1:
        path = path.rstrip("/")
    return f"https://{host}{path or '/'}"


def path_of(url: str) -> str:
    return urllib.parse.urlsplit(normalize(url)).path


def language(url: str) -> str:
    """'no', 'sv' or 'en' from the locale folder (EN at root, /no-no/, /sv-se/)."""
    first = path_of(url).strip("/").split("/", 1)[0].lower()
    return LOCALE_PREFIXES.get(first, "en")


def doc_id(*parts: object) -> str:
    """Deterministic Firestore-safe document id for a composite key."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


BRAND_TERMS = ("corvenia", "corvena", "corveina", "corvinia", "corvenja", "korvenia")


def is_branded(query: str) -> bool:
    q = (query or "").lower().replace(" ", "")
    return any(t in q for t in BRAND_TERMS)
