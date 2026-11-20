"""Small HTTP helpers that never raise: they return (status, payload); status 0 = network error."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

UA = "CorveniaSearchMonitor/2.0 (+https://www.corvenia.com)"


def get(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:  # timeouts, DNS, resets
        return 0, f"{type(e).__name__}: {e}".encode()[:300], {}


def get_json(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, dict | list]:
    code, body, _ = get(url, headers=headers, timeout=timeout)
    try:
        return code, json.loads(body) if body else {}
    except Exception:
        return code, {"error": body.decode(errors="replace")[:500]}


def send_json(url: str, payload: dict | None, headers: dict | None = None, timeout: int = 30,
              method: str = "POST") -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
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
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"[:300]}


post_json = send_json
