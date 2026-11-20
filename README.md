# corvenia-search-monitor

Two things live in this repo:

1. **Marketing Control app** (Google Cloud): search and AI visibility for corvenia.com, with actions.
   Spec: *Corvenia Search & Visibility Insights — Module Spec*. Described first below.
2. **Legacy index monitor** (`monitor.py` + `.github/workflows/daily.yml`): kept running until the
   Cloud Run collectors are verified, then its schedule is disabled (manual `workflow_dispatch` stays).

# Marketing Control app

```
core/        config.py (env + Secret Manager), urls.py (URL join key), store.py (Firestore | JSON),
             rules.py (alerts -> action queue), google_auth.py, http.py
collectors/  indexing.py (port of monitor.py), gsc.py, bing.py, clarity.py, webflow.py, __main__.py
app/         FastAPI app: main.py, queries.py (7/28/90-day views), charts.py, templates/, static/
infra/       Dockerfile, requirements.txt, setup.sh (one-time gcloud setup)
.github/workflows/deploy.yml   push to main -> tests -> build -> Cloud Run service + job
tests/       pytest, JSON store, no network
```

| Piece | Where |
|---|---|
| Web app | Cloud Run service `marketing-control`, europe-north1, behind Identity-Aware Proxy |
| Collectors | Cloud Run Job `collectors` (`python -m collectors all`), same image |
| Schedule | Cloud Scheduler `collectors-daily`, **06:00 Europe/Oslo**, in europe-west1 (Scheduler has no europe-north1) |
| Store | Firestore (default) database, native mode, europe-north1 |
| Secrets | Secret Manager, project `corvenia-search-monitor` |
| Runtime identity | `index-monitor@corvenia-search-monitor.iam.gserviceaccount.com` |

App v1 modules: 1 Scorecard (`/`), 2 Index coverage with *Resubmit to Bing* (`/coverage`),
3 Search performance (`/performance`), 8 Change log with *Log change* (`/changes`).
Period switcher 7 / 28 / 90 days vs. the previous period.

## Secrets (Secret Manager, exact names)

| Secret | Value | Used by |
|---|---|---|
| `BING_API_KEY` | Bing Webmaster Tools → Settings → API access | indexing, bing, app (resubmit) |
| `GSC_SERVICE_ACCOUNT_JSON` | JSON key of `index-monitor@…` (optional on Cloud Run: without it the runtime service account is used directly) | indexing, gsc |
| `CLARITY_API_TOKEN` | Clarity → Settings → Data Export → generate token | clarity |
| `WEBFLOW_API_TOKEN` | Webflow site token with `sites:read pages:read cms:read forms:read` (add `pages:write sites:write` for metadata editing later) | webflow |

Create each with the value read from a file (never on the command line):

```bash
gcloud secrets create BING_API_KEY --replication-policy=automatic --data-file=bing.txt
```

A collector whose secret is missing logs `skipped` and the others run; it does not fail the job.

## Collectors

| Collector | Writes | Notes |
|---|---|---|
| `webflow` | `webflow_pages`, `changes` (publish), enriches `url_inventory` | site + CMS publish dates |
| `indexing` | `url_inventory`, `index_status` (URL × engine × day), `changes` (submit) | Inspection: 60 s timeout, 1 retry, cap 80/run. Bing: ≥1.5 s pacing, 10/20/30 s back-off |
| `gsc` | `gsc_page_daily` (day × page), `gsc_daily` (day × page × query × country × device) | first run backfills 16 months; then from last day − 3 (GSC lags ~2 days); Pacific-time dates as given |
| `bing` | `bing_daily` (kind = site / query / page) | Bing's own date buckets |
| `clarity` | `clarity_daily`, `clarity_raw` | 2 of the 10 allowed requests/day; last 24 h; **must run daily** |
| `rules` | `actions` | index rules: blocked, engine mismatch, slow publish, not indexed after 14 days |

Run state and errors go to `collector_runs` and show in the app footer.

```bash
python -m collectors all                       # everything, in order
python -m collectors gsc --backfill            # re-run the 16-month backfill
python -m collectors clarity --days 3          # recover up to 72 h after a missed day
python -m collectors import-legacy data/state.json   # one-off: carry over the GitHub Actions register
```

Trigger the scheduled job by hand: `gcloud scheduler jobs run collectors-daily --location europe-west1`.
Pause it: `gcloud scheduler jobs pause collectors-daily --location europe-west1`.

## Run locally (JSON store, no cloud)

```bash
python -m venv .venv && . .venv/Scripts/activate      # macOS/Linux: . .venv/bin/activate
pip install -r infra/requirements.txt pytest httpx
python -m pytest -q
export STORE_BACKEND=json DISABLE_SECRET_MANAGER=1 BING_API_KEY=...   # any secret as an env var
python -m collectors indexing                          # writes .localdata/*.json
uvicorn app.main:app --reload                          # http://127.0.0.1:8000
```

`STORE_BACKEND=json` keeps one file per collection in `.localdata/` (git-ignored). With
`STORE_BACKEND=firestore` and `gcloud auth application-default login`, the same commands use the
cloud store and Secret Manager.

## Deploy

One time: `bash infra/setup.sh` (APIs, Firestore, Artifact Registry, IAM, first build, service with
IAP, job, scheduler, GitHub Workload Identity). Then set the repository variable `GCP_WIF_PROVIDER`
to the value the `github` step prints. From then on every push to `main` deploys (pushes that only
touch `data/`, `docs/`, `monitor.py` or Markdown do not).

Access: IAP allows the accounts in `APP_USERS` (default Daniel). Add someone:
`gcloud iap web add-iam-policy-binding --region europe-north1 --resource-type cloud-run --service marketing-control --member user:EMAIL --role roles/iap.httpsResourceAccessor`.

# Legacy index monitor

Daily monitor of Google and Bing index status for every page in the corvenia.com sitemap.

## What it does (once a day, GitHub Actions)

1. Reads `https://www.corvenia.com/sitemap.xml` (and any child sitemaps) and compares it with the stored register in `data/state.json`.
2. For new or changed pages: fetches the page and checks HTTP status, `noindex` and canonical. Pages that fail are marked **blocked** and not submitted.
3. **Bing** – submits new/changed pages via the Bing Webmaster URL Submission API; reads crawl facts via `GetUrlInfo`.
4. **Google** – reads index status via the Search Console URL Inspection API. (Google offers no general submission API; the sitemap is the submission.)
5. Re-checks each page 1, 3, 7 and 14 days after first discovery, then weekly; confirmed-indexed pages are re-checked every 30 days to catch drop-outs.
6. Writes `data/status.json` and a self-contained dashboard `docs/index.html`, and commits both.
7. Pages still unconfirmed after 14 days are listed under **Needs attention**.

Submitted ≠ indexed. The register keeps the two apart.

## Setup (one time)

### Secrets (Settings → Secrets and variables → Actions → *Secrets*)

| Name | Value |
|---|---|
| `GSC_SERVICE_ACCOUNT_JSON` | Full JSON key of a Google service account that has been added as a user (Full or Restricted) on the Search Console property |
| `BING_API_KEY` | API key from Bing Webmaster Tools → Settings → API access |

### Variables (same page → *Variables*, optional)

| Name | Default |
|---|---|
| `GSC_PROPERTY` | `sc-domain:corvenia.com` (use `https://www.corvenia.com/` if the property is URL-prefix type) |
| `BING_SITE_URL` | `https://www.corvenia.com` |

### Google service account

1. Google Cloud Console → any project → IAM & Admin → Service accounts → Create. No roles needed.
2. Keys → Add key → JSON. Paste the file content into the `GSC_SERVICE_ACCOUNT_JSON` secret.
3. APIs & Services → Enable **Google Search Console API**.
4. Search Console → property → Settings → Users and permissions → Add the service account e-mail (Restricted is enough for inspection).

### Run it

Actions → *Daily index check* → **Run workflow**. The first run registers every page and starts the follow-up schedule.

## Dashboard

`docs/index.html` is regenerated each run with the data embedded. Open it from the repository, or serve it from any static host.
Note: GitHub Pages on a private repository requires a paid plan; on the Free plan, open the file directly or host it elsewhere.

## Limits

- Google URL Inspection API: 2 000 inspections per property per day; the job caps itself at 150 per run (configurable with `MAX_GOOGLE_INSPECTIONS_PER_RUN`).
- Bing: submission quota is shown in Bing Webmaster Tools; the job submits at most 500 URLs per run.
- Neither engine guarantees indexing. The monitor reports what they report.
