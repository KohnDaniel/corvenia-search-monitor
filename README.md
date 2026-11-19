# corvenia-search-monitor

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
