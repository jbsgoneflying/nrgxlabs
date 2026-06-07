#!/usr/bin/env bash
set -e

echo "[v2 entrypoint] launching gunicorn on :8001"

# Single worker for Phase 0 — v2 has very low traffic and the foundation
# brain modules will eventually hold model state in-process. Bump workers
# in Phase 1 once we know the per-process memory footprint.
exec gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:8001 --timeout 120 v2_app.main:app
