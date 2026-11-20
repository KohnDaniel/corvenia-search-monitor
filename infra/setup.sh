#!/usr/bin/env bash
# One-time (idempotent where gcloud allows) setup of corvenia-search-monitor on Google Cloud.
# Run from the repo root after `gcloud auth login` and `gcloud config set project corvenia-search-monitor`.
#   bash infra/setup.sh            # everything
#   bash infra/setup.sh <step>     # one step: apis firestore registry iam build service job scheduler github
set -euo pipefail

PROJECT=corvenia-search-monitor
REGION=europe-north1            # Cloud Run, Firestore, Artifact Registry
SCHED_REGION=europe-west1       # Cloud Scheduler has no europe-north1; it triggers the job across regions
SA=index-monitor@${PROJECT}.iam.gserviceaccount.com          # runtime: GSC user, Firestore, secrets
SCHED_SA=collector-scheduler@${PROJECT}.iam.gserviceaccount.com
DEPLOY_SA=github-deployer@${PROJECT}.iam.gserviceaccount.com
SERVICE=marketing-control
JOB=collectors
REPO=corvenia
IMAGE=${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/marketing-control
GITHUB_REPO=KohnDaniel/corvenia-search-monitor
APP_USERS=${APP_USERS:-user:daniel.m.kohn@gmail.com}   # space-separated IAP allow-list
ENV_VARS="STORE_BACKEND=firestore,GCP_PROJECT=${PROJECT},GSC_PROPERTY=https://www.corvenia.com/,BING_SITE_URL=https://corvenia.com"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
step=${1:-all}
run() { [[ $step == all || $step == "$1" ]]; }

if run apis; then
  gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    firestore.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com iap.googleapis.com \
    searchconsole.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project "$PROJECT"
fi

if run firestore; then
  gcloud firestore databases describe --database='(default)' --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud firestore databases create --database='(default)' --location="$REGION" --type=firestore-native --project "$PROJECT"
fi

if run registry; then
  gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION" --project "$PROJECT"
fi

if run iam; then
  for role in roles/datastore.user roles/secretmanager.secretAccessor roles/logging.logWriter; do
    gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --condition=None -q >/dev/null
  done
  gcloud iam service-accounts describe "$SCHED_SA" --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create collector-scheduler --display-name "Cloud Scheduler -> collectors job" --project "$PROJECT"
  # Cloud Build (first deploy from this machine) builds as the default compute service account.
  for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
    gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
      --role "$role" --condition=None -q >/dev/null
  done
fi

if run build; then
  gcloud builds submit --project "$PROJECT" --region "$REGION" --config /dev/stdin . <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: [build, -f, infra/Dockerfile, -t, ${IMAGE}:latest, .]
images: [${IMAGE}:latest]
EOF
fi

if run service; then
  gcloud run deploy "$SERVICE" --image "${IMAGE}:latest" --region "$REGION" --project "$PROJECT" \
    --service-account "$SA" --no-allow-unauthenticated --iap \
    --set-env-vars "$ENV_VARS" --memory 1Gi --cpu 1 --min-instances 0 --max-instances 2 --timeout 120
  # IAP service agent must be able to invoke the service.
  gcloud beta services identity create --service=iap.googleapis.com --project "$PROJECT" >/dev/null 2>&1 || true
  gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" --project "$PROJECT" \
    --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker
  for u in $APP_USERS; do
    gcloud iap web add-iam-policy-binding --project "$PROJECT" --region "$REGION" --resource-type=cloud-run \
      --service "$SERVICE" --member "$u" --role roles/iap.httpsResourceAccessor
  done
fi

if run job; then
  gcloud run jobs deploy "$JOB" --image "${IMAGE}:latest" --region "$REGION" --project "$PROJECT" \
    --service-account "$SA" --command python --args=-m,collectors,all \
    --set-env-vars "$ENV_VARS" --task-timeout 3600 --max-retries 0 --memory 1Gi --cpu 1
  gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" --project "$PROJECT" \
    --member "serviceAccount:$SCHED_SA" --role roles/run.invoker
fi

if run scheduler; then
  URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
  if gcloud scheduler jobs describe collectors-daily --location "$SCHED_REGION" --project "$PROJECT" >/dev/null 2>&1; then
    verb=update
  else
    verb=create
  fi
  gcloud scheduler jobs $verb http collectors-daily --location "$SCHED_REGION" --project "$PROJECT" \
    --schedule "0 6 * * *" --time-zone "Europe/Oslo" --uri "$URI" --http-method POST \
    --oauth-service-account-email "$SCHED_SA" --attempt-deadline 60s
fi

if run github; then
  # Keyless deploy from GitHub Actions (Workload Identity Federation), limited to this repository.
  gcloud iam service-accounts describe "$DEPLOY_SA" --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create github-deployer --display-name "GitHub Actions deploy" --project "$PROJECT"
  for role in roles/run.admin roles/artifactregistry.writer; do
    gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$DEPLOY_SA" --role "$role" --condition=None -q >/dev/null
  done
  gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" \
    --member "serviceAccount:$DEPLOY_SA" --role roles/iam.serviceAccountUser >/dev/null
  gcloud iam workload-identity-pools describe github --location global --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud iam workload-identity-pools create github --location global --display-name GitHub --project "$PROJECT"
  gcloud iam workload-identity-pools providers describe github --workload-identity-pool github --location global --project "$PROJECT" >/dev/null 2>&1 ||
    gcloud iam workload-identity-pools providers create-oidc github --workload-identity-pool github --location global \
      --issuer-uri https://token.actions.githubusercontent.com \
      --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
      --attribute-condition "assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'" --project "$PROJECT"
  gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --project "$PROJECT" --role roles/iam.workloadIdentityUser \
    --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}" >/dev/null
  echo "WIF provider: projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github"
fi

echo "done: $step"
