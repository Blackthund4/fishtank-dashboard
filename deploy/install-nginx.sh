#!/usr/bin/env bash
# install-nginx.sh
#
# One-time-ish setup script for the nginx reverse proxy on the VPS. Idempotent
# where possible — re-running won't break things, but the cert install step
# expects /etc/nginx/certs/origin.{crt,key} to already exist (generated from
# the Cloudflare dashboard).
#
# Run on the VPS as root after `git pull` of the feat/nginx-reverse-proxy
# branch (or main, post-merge):
#
#     bash deploy/install-nginx.sh
#
# Does NOT start/restart services on its own — orchestration (docker compose
# down, nginx reload) is left to the human running the deploy so the order is
# explicit and the human is paying attention.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SITE_CONF_SRC="$REPO_ROOT/nginx/fish-dash.conf"
SITE_CONF_DST="/etc/nginx/sites-available/fish-dash.conf"
SITE_ENABLED_LINK="/etc/nginx/sites-enabled/fish-dash.conf"
DEFAULT_ENABLED_LINK="/etc/nginx/sites-enabled/default"
CERT_DIR="/etc/nginx/certs"
CERT_FILE="$CERT_DIR/origin.crt"
KEY_FILE="$CERT_DIR/origin.key"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)." >&2
    exit 1
fi

echo "==> Installing nginx (if not already present)"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx

echo "==> Verifying cert files exist at $CERT_DIR"
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    cat >&2 <<EOF
ERROR: cert files not found.
Generate a Cloudflare Origin Certificate from the CF dashboard
(SSL/TLS -> Origin Server -> Create Certificate, ECDSA, 15-year validity,
hostnames: fish-dash.com, *.fish-dash.com) and place them at:
    $CERT_FILE
    $KEY_FILE
Then re-run this script.
EOF
    exit 1
fi

echo "==> Locking down cert permissions"
chown root:root "$CERT_FILE" "$KEY_FILE"
chmod 644 "$CERT_FILE"
chmod 600 "$KEY_FILE"

echo "==> Installing site config to $SITE_CONF_DST"
install -m 0644 "$SITE_CONF_SRC" "$SITE_CONF_DST"

echo "==> Enabling site and disabling default"
ln -sfn "$SITE_CONF_DST" "$SITE_ENABLED_LINK"
rm -f "$DEFAULT_ENABLED_LINK"

echo "==> Testing nginx config"
nginx -t

cat <<EOF

==> install-nginx.sh complete.

Next steps (run by hand so the cutover is explicit):

  1) Stop the old api container that owns port 443:
       cd /opt/fishtank-dashboard
       docker compose stop api

  2) Apply the new docker-compose (api now binds 127.0.0.1:8000):
       GIT_COMMIT=\$(git rev-parse --short HEAD) docker compose up -d --build api

  3) Verify uvicorn is up on loopback:
       curl -sf http://127.0.0.1:8000/api/health

  4) Start nginx:
       systemctl enable --now nginx
       # or, if already running:  systemctl reload nginx

  5) Verify the full chain from the host:
       curl -kI https://localhost/api/health

  6) From your dev PC, verify the bypass is closed and CF traffic works.
EOF
