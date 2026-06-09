#!/usr/bin/env bash
set -e

# Re-seed static reference data into the persistent /app/data volume.
# /app/data is a named volume that is only populated from the image the first
# time it's created, so new/updated static files baked into the image would
# otherwise never reach production. We force-refresh only the static universe
# reference files (lists + sector map); runtime caches (SQLite, etc.) elsewhere
# under the volume are left untouched.
if [ -d /app/seed-data/universe ]; then
  mkdir -p /app/data/universe
  cp -f /app/seed-data/universe/*.txt /app/data/universe/ 2>/dev/null || true
  cp -f /app/seed-data/universe/*.json /app/data/universe/ 2>/dev/null || true
  echo "[entrypoint] Seeded static universe reference files into /app/data/universe."
fi

# Install the crontab (env vars are inherited via env dump)
env >> /etc/environment
crontab /app/deploy/crontab
service cron start

echo "[entrypoint] Cron started. Launching gunicorn..."

# Engine 1 + Monte Carlo + ORATS/EODHD can exceed 120s on cold paths; keep >= nginx proxy_read_timeout.
exec gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 --timeout 240 backend.app:app
