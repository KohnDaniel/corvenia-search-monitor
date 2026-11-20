#!/usr/bin/env bash
# Attach a custom OAuth client to IAP on the Cloud Run service (required in a project without an
# organisation; Google does not allow creating that client from the command line).
#   bash infra/iap-oauth.sh path/to/client_secret_XXXX.json
# The JSON is the file "Download JSON" gives for a Web application OAuth client. Its secret is never
# printed; the temporary settings file is deleted on exit.
set -euo pipefail
JSON=${1:?path to the downloaded OAuth client JSON}
PROJECT=corvenia-search-monitor
REGION=europe-north1
SERVICE=marketing-control
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
PY=${CLOUDSDK_PYTHON:-python}
"$PY" - "$JSON" "$TMP" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))["web"]
open(sys.argv[2], "w").write(
    "access_settings:\n  oauth_settings:\n"
    f"    client_id: {c['client_id']}\n    client_secret: {c['client_secret']}\n")
print("client id:", c["client_id"])
EOF
gcloud iap settings set "$TMP" --project "$PROJECT" --resource-type=cloud-run --region "$REGION" --service "$SERVICE" >/dev/null
echo "IAP now uses the custom OAuth client for $SERVICE."
