#!/usr/bin/env bash
# nrgxlabs.com migration — Phase 2a (nginx vhost installation).
#
# Run on the droplet AFTER `git pull` has fetched the new vhost configs.
# Idempotent — safe to re-run if anything fails halfway.
#
# What it does:
#   1. Pulls latest code from main (in case it wasn't fresh)
#   2. Installs both nginx vhost configs into sites-available
#   3. Symlinks them into sites-enabled
#   4. Tests nginx config; aborts if invalid
#   5. Reloads nginx (graceful — no dropped connections)
#   6. Verifies HTTP 200 from both new hostnames
#
# What it does NOT do:
#   - Provision TLS certificates (run certbot manually after this — see
#     deploy/nrgxlabs-migration.md Phase 2b for the two commands).
#
# Historical note: during the migration this script also coexisted with
# the legacy `raven-tech.co` vhosts. That domain has since been retired
# and is no longer registered to us, so only the `nrgxlabs.com` vhosts
# remain.

set -euo pipefail

REPO_ROOT="/opt/breach-algo"
NGINX_AVAIL="/etc/nginx/sites-available"
NGINX_ENABLED="/etc/nginx/sites-enabled"

ROOT_CONF="site-root.nrgxlabs.com.conf"
APP_CONF="site-app.nrgxlabs.com.conf"

echo "─── nrgxlabs.com Phase 2a ───────────────────────────────"
echo "repo:     $REPO_ROOT"
echo "host:     $(hostname)"
echo "user:     $(whoami)"
echo

if [[ ! -d "$REPO_ROOT" ]]; then
  echo "FATAL: $REPO_ROOT not found. Are you on the right droplet?" >&2
  exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "FATAL: must be run as root (use 'sudo bash $0')." >&2
  exit 1
fi

echo "[1/6] Pulling latest from main..."
cd "$REPO_ROOT"
git pull origin main
echo

echo "[2/6] Verifying source vhost configs are present..."
for f in "$ROOT_CONF" "$APP_CONF"; do
  src="$REPO_ROOT/deploy/nginx/$f"
  if [[ ! -f "$src" ]]; then
    echo "FATAL: $src missing from repo. Did the commit reach main?" >&2
    exit 1
  fi
  echo "  ✓ $src"
done
echo

echo "[3/6] Installing vhost configs into $NGINX_AVAIL..."
cp "$REPO_ROOT/deploy/nginx/$ROOT_CONF" "$NGINX_AVAIL/nrgxlabs.com"
cp "$REPO_ROOT/deploy/nginx/$APP_CONF"  "$NGINX_AVAIL/app.nrgxlabs.com"
echo "  ✓ $NGINX_AVAIL/nrgxlabs.com"
echo "  ✓ $NGINX_AVAIL/app.nrgxlabs.com"
echo

echo "[4/6] Symlinking into $NGINX_ENABLED..."
ln -sf "$NGINX_AVAIL/nrgxlabs.com"     "$NGINX_ENABLED/nrgxlabs.com"
ln -sf "$NGINX_AVAIL/app.nrgxlabs.com" "$NGINX_ENABLED/app.nrgxlabs.com"
echo "  ✓ $NGINX_ENABLED/nrgxlabs.com -> $NGINX_AVAIL/nrgxlabs.com"
echo "  ✓ $NGINX_ENABLED/app.nrgxlabs.com -> $NGINX_AVAIL/app.nrgxlabs.com"
echo

echo "[5/6] Validating nginx configuration..."
if ! nginx -t; then
  echo
  echo "FATAL: nginx config test failed. Configuration NOT applied." >&2
  echo "Disable the new vhosts and retry:" >&2
  echo "  sudo rm $NGINX_ENABLED/nrgxlabs.com $NGINX_ENABLED/app.nrgxlabs.com" >&2
  exit 1
fi
echo "  ✓ nginx -t passed"
echo

echo "[6/6] Reloading nginx (graceful)..."
systemctl reload nginx
echo "  ✓ reloaded"
echo

echo "─── Verification ────────────────────────────────────────"
sleep 1
for host in nrgxlabs.com app.nrgxlabs.com; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: $host" "http://127.0.0.1/" || echo "ERR")
  if [[ "$code" == "200" ]]; then
    echo "  ✓ http://$host/  → $code"
  else
    echo "  ✗ http://$host/  → $code  (investigate before running certbot)"
  fi
done
echo

cat <<'NEXT'
─── Phase 2a complete. Next: Phase 2b (TLS) ─────────────
Run these two commands (each will prompt — pick option 2 to redirect HTTP→HTTPS):

  sudo certbot --nginx -d nrgxlabs.com -d www.nrgxlabs.com
  sudo certbot --nginx -d app.nrgxlabs.com

Then verify from your laptop:
  curl -sI https://nrgxlabs.com/ | head -1
  curl -sI https://app.nrgxlabs.com/api/health
NEXT
