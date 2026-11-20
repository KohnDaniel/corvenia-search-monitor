"""Configuration from environment variables, with secrets read from Google Secret Manager.

Secret lookup order for a name such as BING_API_KEY:
  1. Environment variable of the same name (local development, tests).
  2. Secret Manager secret of the same name in GCP_PROJECT, version "latest".
  3. Empty string (the collector that needs it disables itself and says so).
"""

from __future__ import annotations

import functools
import os

GCP_PROJECT = os.environ.get("GCP_PROJECT", "corvenia-search-monitor")
SITE_URL = os.environ.get("SITE_URL", "https://www.corvenia.com").rstrip("/")
SITEMAP_URL = os.environ.get("SITEMAP_URL", f"{SITE_URL}/sitemap.xml")
# Search Console property is URL-prefix with www (there is no domain property).
GSC_PROPERTY = os.environ.get("GSC_PROPERTY", "https://www.corvenia.com/")
# Bing holds corvenia.com as a domain-level site; SubmitUrlBatch works with the bare domain.
BING_SITE_URL = os.environ.get("BING_SITE_URL", "https://corvenia.com")
WEBFLOW_SITE_ID = os.environ.get("WEBFLOW_SITE_ID", "6915949bf0363df94e66ef9a")
TIMEZONE = "Europe/Oslo"

# Store: "firestore" in Cloud Run, "json" locally (default when no GCP credentials are wanted).
STORE_BACKEND = os.environ.get("STORE_BACKEND", "json")
JSON_STORE_DIR = os.environ.get("JSON_STORE_DIR", ".localdata")

MAX_GOOGLE_INSPECTIONS_PER_RUN = int(os.environ.get("MAX_GOOGLE_INSPECTIONS_PER_RUN", "80"))
MAX_BING_CHECKS_PER_RUN = int(os.environ.get("MAX_BING_CHECKS_PER_RUN", "80"))
BING_PAUSE_SECONDS = float(os.environ.get("BING_PAUSE_SECONDS", "1.5"))

# Names of the secrets this app reads. Create them in Secret Manager with exactly these names.
SECRET_NAMES = ("BING_API_KEY", "GSC_SERVICE_ACCOUNT_JSON", "CLARITY_API_TOKEN", "WEBFLOW_API_TOKEN")


@functools.lru_cache(maxsize=None)
def secret(name: str) -> str:
    val = os.environ.get(name, "")
    if val:
        return val
    if os.environ.get("DISABLE_SECRET_MANAGER") == "1":
        return ""
    try:
        from google.cloud import secretmanager  # type: ignore

        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{GCP_PROJECT}/secrets/{name}/versions/latest"
        return client.access_secret_version(name=path).payload.data.decode("utf-8").strip()
    except Exception as e:  # not created yet, no permission, or no credentials locally
        print(f"[config] secret {name} unavailable: {type(e).__name__}: {str(e)[:160]}", flush=True)
        return ""
