# One image, two entrypoints:
#   service: uvicorn app.main:app           (default CMD)
#   job:     python -m collectors all       (Cloud Run Job overrides the command)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 STORE_BACKEND=firestore
WORKDIR /srv

COPY infra/requirements.txt /srv/infra/requirements.txt
RUN pip install --no-cache-dir -r /srv/infra/requirements.txt

COPY core /srv/core
COPY collectors /srv/collectors
COPY app /srv/app
# Snapshot of the GitHub Actions register, for the one-off `python -m collectors import-legacy`.
COPY data/state.json /srv/data/state.json

RUN useradd --uid 10001 --no-create-home app
USER app

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
