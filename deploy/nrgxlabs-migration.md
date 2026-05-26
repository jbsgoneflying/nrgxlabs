# NRGX Labs domain migration runbook

Step-by-step commands for moving the live site from `raven-tech.co` to
`nrgxlabs.com`. Designed to be paste-runnable on the droplet, in order,
with verification gates at each phase. **Both domains run side-by-side**
during the migration so nothing breaks while we test.

---

## Status before you begin

- [x] **Phase 1 — Domain registration** (done in GoDaddy)
  - `nrgxlabs.com` registered and DNS pointed at `165.245.143.2`
  - A records: apex, `app`, and `www` all resolve correctly (verified `dig +short`)
  - **TODO:** add MX records for `desk@nrgxlabs.com` email forwarding
    (GoDaddy → Email & Office → Forwarding). Not blocking — do this any
    time. Until then, `mailto:desk@nrgxlabs.com` will land nowhere.

The rest of this doc is Phases 2–5.

---

## Phase 2 — Server config (≈10 min, requires SSH)

### 2a. Install nginx vhosts (one paste)

A single setup script handles the whole vhost installation. It's
idempotent (safe to re-run if anything fails halfway) and verifies
the config before reloading nginx.

SSH into the droplet and run:

```bash
ssh root@165.245.143.2
sudo bash /opt/breach-algo/deploy/nrgxlabs-phase2-setup.sh
```

What it does:
1. `git pull origin main` (fetches the new vhost files)
2. Copies both vhost configs into `/etc/nginx/sites-available/`
3. Symlinks them into `sites-enabled/`
4. Runs `nginx -t` (aborts if invalid)
5. Reloads nginx
6. Verifies HTTP 200 from both new hostnames

If step 4 fails, the script aborts before touching the running nginx
config, so the existing `raven-tech.co` setup keeps serving traffic.

**Expected output (last 4 lines):**

```
Verification ────────────────────────────────────────
  ✓ http://nrgxlabs.com/  → 200
  ✓ http://app.nrgxlabs.com/  → 200
```

If either shows anything but 200, stop and check `sudo journalctl -u nginx -n 50`.

---

### 2b. Provision TLS certificates with certbot

Two domains, two certbot runs. Each interactive prompt: pick option `2`
(redirect HTTP→HTTPS) when asked.

```bash
# Splash domain (apex + www on a single cert).
sudo certbot --nginx -d nrgxlabs.com -d www.nrgxlabs.com

# App subdomain.
sudo certbot --nginx -d app.nrgxlabs.com
```

Certbot will rewrite the two vhost files to add the `listen 443 ssl;`
blocks and an HTTP→HTTPS redirect (matching the existing
`site-app.raven-tech.co.conf` pattern). Don't manually edit the files
afterwards — let certbot manage them.

**Verify (from your laptop, not the droplet):**

```bash
# Both domains should serve valid certs and redirect HTTP→HTTPS.
curl -sI https://nrgxlabs.com/ | head -1
#   → HTTP/2 200

curl -sI https://app.nrgxlabs.com/api/health
#   → HTTP/2 200  (and body: {"ok":true,...})

curl -sI http://nrgxlabs.com/ | grep -i location
#   → location: https://nrgxlabs.com/

# Cert details — should show Let's Encrypt and 90-day expiry.
echo | openssl s_client -servername nrgxlabs.com -connect nrgxlabs.com:443 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

✅ **Phase 2 done when:** `https://nrgxlabs.com/` shows the splash and
`https://app.nrgxlabs.com/` shows the app login screen, both with
valid TLS.

---

## Phase 3 — Smoke test the new domain (≈5 min)

Click through the app on the new hostname and confirm everything works.
The two domains share the same backend, so any data you create here is
real — keep that in mind.

- [ ] Visit https://nrgxlabs.com — splash loads, logo, ledger, "enter the desk" button works
- [ ] Click "Enter the desk" — lands on https://app.nrgxlabs.com/login
- [ ] Log in with invite code `RAVEN-BETA-2026` — should succeed
- [ ] Navigate Engine 1 (`/earnings-iv-crush`), Engine 2 (`/spx-ic`),
      Engine 14 (`/spx-scenario`), MI (`/regime-intel`) — each loads
- [ ] Hit `/flow-monitor` — Next.js app loads
- [ ] Open browser devtools → Network tab → confirm session cookies are
      set on `app.nrgxlabs.com` and not leaking from `raven-tech.co`

If sessions don't persist or CORS errors show up in the console, jump
to **Phase 4** (app-side config) below — the backend may have hardcoded
host/cookie domains that need adjusting.

✅ **Phase 3 done when:** every page that worked on `raven-tech.co`
works identically on `nrgxlabs.com`.

---

## Phase 4 — Backend config (only if Phase 3 surfaced issues)

If sessions or CORS broke on the new domain, the FastAPI backend likely
has hardcoded references to `raven-tech.co`. Fix from your laptop:

```bash
# In the repo, audit for hardcoded references.
rg "raven-tech\.co" backend/ static/
```

Common culprits and fixes:

| Symptom | File | Fix |
|---|---|---|
| Session doesn't persist on new domain | `backend/app.py` (look for `SessionMiddleware` / `set_cookie`) | Either omit `domain=` (cookie scoped to current host) or read from env |
| CORS errors in console | `backend/app.py` (`CORSMiddleware` config) | Add `https://app.nrgxlabs.com` to allowed origins |
| Email/password reset links point to old domain | `backend/email.py` or similar | Read base URL from env, set `PRIMARY_HOST=app.nrgxlabs.com` in droplet `.env` |
| Hardcoded canonical link in HTML head | `static/*.html` | Replace with relative link or env-templated value |

After fixes:

```bash
git add backend/ static/
git commit -m "chore: accept app.nrgxlabs.com as a primary host"
git push origin main   # GitHub Actions will deploy in ~90s
```

Re-run Phase 3 smoke tests.

✅ **Phase 4 done when:** sessions, CORS, and any URL-generating code all
work cleanly on `app.nrgxlabs.com`.

---

## Phase 5 — Cut over (when you're ready to retire raven-tech.co)

Only do this once Phase 3 is fully green. This step makes
`raven-tech.co` a permanent 301 redirect to `nrgxlabs.com` — search
engines will start moving authority over within a few weeks.

### 5a. Replace the old vhosts with redirects

On the droplet:

```bash
# Back up the existing configs first (just in case).
sudo cp /etc/nginx/sites-available/raven-tech.co     /etc/nginx/sites-available/raven-tech.co.bak
sudo cp /etc/nginx/sites-available/app.raven-tech.co /etc/nginx/sites-available/app.raven-tech.co.bak

# Replace each with a single permanent-redirect server block.
sudo tee /etc/nginx/sites-available/raven-tech.co > /dev/null <<'EOF'
server {
  listen 80;
  listen 443 ssl;
  server_name raven-tech.co www.raven-tech.co;
  ssl_certificate     /etc/letsencrypt/live/raven-tech.co/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/raven-tech.co/privkey.pem;
  return 301 https://nrgxlabs.com$request_uri;
}
EOF

sudo tee /etc/nginx/sites-available/app.raven-tech.co > /dev/null <<'EOF'
server {
  listen 80;
  listen 443 ssl;
  server_name app.raven-tech.co;
  ssl_certificate     /etc/letsencrypt/live/app.raven-tech.co-0001/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/app.raven-tech.co-0001/privkey.pem;
  return 301 https://app.nrgxlabs.com$request_uri;
}
EOF

sudo nginx -t && sudo systemctl reload nginx
```

**Verify:**

```bash
curl -sI https://app.raven-tech.co/login | grep -iE 'HTTP|location'
#   → HTTP/2 301
#   → location: https://app.nrgxlabs.com/login
```

### 5b. Update the deploy workflow's health check

The GitHub Action in `.github/workflows/deploy.yml` references
`raven-tech.co` in its post-deploy verification curl. Switch it to
`nrgxlabs.com` so future deploys verify the canonical hostname.

(Already on your todo list — separate PR.)

### 5c. UI rebrand

Sweep the static frontend for "Raven Tech" → "NRGX Labs" in:

- Page titles and headers
- The login screen
- Any hardcoded logos / favicons
- README and any user-facing docs

(Separate PR; not blocking the migration.)

---

## Rollback (if anything goes wrong)

Phases 2–4 are non-destructive — `raven-tech.co` keeps working
throughout. To undo any phase:

```bash
# Disable the new vhosts entirely.
sudo rm /etc/nginx/sites-enabled/nrgxlabs.com
sudo rm /etc/nginx/sites-enabled/app.nrgxlabs.com
sudo systemctl reload nginx
```

If Phase 5 cutover causes issues, restore from the backups created in 5a:

```bash
sudo cp /etc/nginx/sites-available/raven-tech.co.bak     /etc/nginx/sites-available/raven-tech.co
sudo cp /etc/nginx/sites-available/app.raven-tech.co.bak /etc/nginx/sites-available/app.raven-tech.co
sudo systemctl reload nginx
```

---

## Quick reference: file locations

| File | Path on droplet | Purpose |
|---|---|---|
| Splash vhost | `/etc/nginx/sites-available/nrgxlabs.com` | Static splash |
| App vhost | `/etc/nginx/sites-available/app.nrgxlabs.com` | App proxy |
| Splash HTML | `/opt/breach-algo/nrgxlabs-splash/index.html` | Auto-updated by GH Actions |
| TLS certs | `/etc/letsencrypt/live/{nrgxlabs.com,app.nrgxlabs.com}/` | Managed by certbot |

`certbot renew` runs nightly via systemd timer (`systemctl list-timers | grep certbot`) — no manual renewal needed.
