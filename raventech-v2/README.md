# NRGX Labs v2

A side-by-side rebuild of the NRGX Labs trading platform on top of a learned
Foundation Brain and an agentic Claude reasoning layer. v1 keeps trading at
[`app.nrgxlabs.com`](https://app.nrgxlabs.com); v2 grows up alongside it at
[`v2.app.nrgxlabs.com`](https://v2.app.nrgxlabs.com).

The full architecture is in `/Users/.../.cursor/plans/raven_tech_v2_architecture_*.plan.md`.
Phase 0 (this directory) is the scaffolding: separate FastAPI service, distinct
visual identity, counterfactual logger sidecar, and stub endpoints that
advertise the Foundation Brain shape.

## Layers

```
v2_app/
  routers/        FastAPI HTTP routes (/api/v2/*)
  foundation/     Layer 1 — learned regime / analogue / path / conformal models
  engines/        Layer 2 — E1/E2/E14/E15/MI v2 cores built on foundation
  agents/         Layer 3 — Researcher → Quant → Devil → Risk → Synthesizer
  eval/           Layer 4 — counterfactual journal, conformal coverage tracker

static-v2/        Frontend (vanilla JS, modern CSS, no bundler)
deploy/           Container entrypoint
tests/            Phase 0 smoke tests
```

## Running locally

```bash
# From the repo root, in the existing .venv:
.venv/bin/pip install -r raventech-v2/requirements-v2.txt
PUBLIC_ACCESS=1 V2_BIND_PORT=8001 .venv/bin/python -m uvicorn v2_app.main:app \
  --reload --port 8001 --app-dir raventech-v2
```

Then open `http://localhost:8001/`.

## Tests

```bash
.venv/bin/python -m pytest raventech-v2/tests -q
```

## Container

```bash
docker compose up -d --build app-v2
curl -sf http://localhost:8001/api/v2/health
```

`docker-compose.yml` defines the `app-v2` service alongside the v1 `app`. They
share Redis; v2 has read-only access to the v1 chain-cache volume.

## Going live at `v2.app.nrgxlabs.com`

Three manual steps on the droplet (one-time):

1. **DNS** — Add an A record for `v2.app.nrgxlabs.com` pointing at the droplet IP
   (DigitalOcean → Networking → Domains → `nrgxlabs.com` → Add A record).
2. **nginx** —
   ```bash
   sudo cp deploy/nginx/site-v2.app.nrgxlabs.com.conf \
           /etc/nginx/sites-available/v2.app.nrgxlabs.com
   sudo ln -s /etc/nginx/sites-available/v2.app.nrgxlabs.com \
              /etc/nginx/sites-enabled/v2.app.nrgxlabs.com
   sudo nginx -t && sudo systemctl reload nginx
   ```
3. **HTTPS** —
   ```bash
   sudo certbot --nginx -d v2.app.nrgxlabs.com
   ```

Until those land, v2 is verifiable via SSH on the droplet:
`curl -sf http://localhost:8001/api/v2/health`. The GitHub Actions deploy
hits that endpoint automatically and prints PASS/FAIL.

## Auth

v2 reuses v1's HMAC-signed invite-code cookie. A desk member who logs into
`app.nrgxlabs.com` is automatically authenticated on `v2.app.nrgxlabs.com`
because the cookie is set on the parent domain. v2 boots in `PUBLIC_ACCESS=1`
mode by default; set `PUBLIC_ACCESS=0` (and ensure `INVITE_CODE` +
`AUTH_SECRET` are set) to engage the gate.

## What's next (Phase 1)

The first user-visible win is the contrastive analogue embedder. See the plan
file for the full phasing — but the wedge for "look, v2 is already smarter
than v1" is `/api/v2/analogues/search` returning real cross-ticker neighbors
for an upcoming earnings event.
