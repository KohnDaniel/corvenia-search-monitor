"""Google credentials for Search Console.

Uses the GSC_SERVICE_ACCOUNT_JSON secret when present (same key as the GitHub Actions job).
Otherwise falls back to Application Default Credentials, which on Cloud Run is the runtime
service account index-monitor@corvenia-search-monitor.iam.gserviceaccount.com (already a
Restricted user on the Search Console property).
"""

from __future__ import annotations

import json
import time

from core import config

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

_creds = None
_who = ""


def token() -> str:
    """Return a valid access token, refreshing when it is near expiry. google-auth needs `requests`."""
    global _creds, _who
    from google.auth.transport.requests import Request  # type: ignore

    if _creds is None:
        raw = config.secret("GSC_SERVICE_ACCOUNT_JSON")
        if raw:
            from google.oauth2 import service_account  # type: ignore

            info = json.loads(raw)
            _creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            _who = info.get("client_email", "service account key")
        else:
            import google.auth  # type: ignore

            _creds, _ = google.auth.default(scopes=SCOPES)
            _who = getattr(_creds, "service_account_email", "application default credentials")
    if not _creds.valid or (_creds.expiry and _creds.expiry.timestamp() - time.time() < 300):
        _creds.refresh(Request())
    return _creds.token


def identity() -> str:
    return _who
