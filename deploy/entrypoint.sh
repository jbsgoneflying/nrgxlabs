#!/usr/bin/env bash
set -e

# Install the crontab (env vars are inherited via env dump)
env >> /etc/environment
crontab /app/deploy/crontab
service cron start

echo "[entrypoint] Cron started. Launching gunicorn..."

# Engine 1 + Monte Carlo + ORATS/EODHD can exceed 120s on cold paths; keep >= nginx proxy_read_timeout.
exec gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 --timeout 240 backend.app:app
