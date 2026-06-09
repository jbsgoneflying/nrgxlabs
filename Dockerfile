FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: curl for health checks, cron for scheduled jobs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    cron \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend /app/backend
COPY static /app/static
COPY scripts /app/scripts
COPY data /app/data
# Baked-in seed copy of the repo's static data. /app/data is shadowed at runtime
# by a persistent named volume (see docker-compose.yml), so new/updated static
# reference files (universe lists, sector map) won't appear there on redeploy.
# The entrypoint re-seeds them from /app/seed-data into the volume on each boot.
COPY data /app/seed-data
COPY deploy /app/deploy

RUN chmod +x /app/deploy/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
