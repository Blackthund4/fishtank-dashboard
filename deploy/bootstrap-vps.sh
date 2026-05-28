#!/usr/bin/env bash
# bootstrap-vps.sh
#
# One-shot setup script for a fresh Ubuntu 24.04 VPS that will run the
# fishtank-dashboard stack. Designed to be run once on a brand-new instance
# (e.g., post-IP-rotation, or disaster recovery). Idempotent where reasonable
# but not designed for repeat-on-running-system use.
#
# Usage on a new VPS (after SSH key is in place):
#   ssh root@<new-ip>
#   git clone https://github.com/Blackthund4/fishtank-dashboard /opt/fishtank-dashboard
#   cd /opt/fishtank-dashboard
#   bash deploy/bootstrap-vps.sh
#
# After this script completes, manual steps still needed:
#   1. scp certs/origin.{crt,key} from PC to /etc/nginx/certs/
#   2. Run deploy/install-nginx.sh
#   3. Restore fishtank.db from old VPS (see docs)
#   4. Start containers: GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d
#   5. Start nginx: systemctl enable --now nginx

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root." >&2
    exit 1
fi

echo "==> [1/6] System update"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y

echo "==> [2/6] Install base packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    curl \
    git \
    gnupg \
    lsb-release \
    ufw \
    fail2ban \
    nginx

echo "==> [3/6] Install Docker (official apt repo)"
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

echo "==> [4/6] SSH hardening (key-only auth, no root password)"
tee /etc/ssh/sshd_config.d/01-hardening.conf > /dev/null <<'EOF'
# Hardening overrides — read before 50-cloud-init.conf alphabetically
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PermitEmptyPasswords no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
EOF
chmod 644 /etc/ssh/sshd_config.d/01-hardening.conf
sshd -t
systemctl restart ssh

echo "==> [5/6] fail2ban jail for sshd"
tee /etc/fail2ban/jail.local > /dev/null <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 3
backend  = systemd

[sshd]
enabled = true
port    = 22
EOF
systemctl enable --now fail2ban

echo "==> [6/6] UFW: SSH globally (key-only + fail2ban), 443 from Cloudflare IPs only"

# Default policies
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp

# Pull current Cloudflare ranges. Bail if the fetch fails — we'd rather not
# silently end up with zero 443 rules.
CF_V4="$(curl -fsS https://www.cloudflare.com/ips-v4/ || true)"
CF_V6="$(curl -fsS https://www.cloudflare.com/ips-v6/ || true)"
if [ -z "$CF_V4" ] || [ -z "$CF_V6" ]; then
    echo "ERROR: failed to fetch Cloudflare IP lists; refusing to enable UFW with no 443 rules." >&2
    exit 1
fi

while IFS= read -r cidr; do
    [ -n "$cidr" ] || continue
    ufw allow from "$cidr" to any port 443 proto tcp comment 'CF v4'
done <<< "$CF_V4"

while IFS= read -r cidr; do
    [ -n "$cidr" ] || continue
    ufw allow from "$cidr" to any port 443 proto tcp comment 'CF v6'
done <<< "$CF_V6"

ufw --force enable
ufw status numbered | head -40

cat <<EOF

==> bootstrap-vps.sh complete.

Verify Docker is working:
  docker run --rm hello-world

Then continue with manual steps:
  1. mkdir /etc/nginx/certs
  2. From PC: scp certs/origin.{crt,key} <new-vps>:/etc/nginx/certs/
  3. bash deploy/install-nginx.sh
  4. Restore DB:
       (on old VPS)  docker compose exec api sqlite3 /app/data/fishtank.db ".backup /tmp/fishtank.db.fresh"
       (from PC)     scp old-vps:/tmp/fishtank.db.fresh new-vps:/tmp/
       (on new VPS)  mkdir -p /var/lib/docker/volumes/fishtank-dashboard_fishtank-data/_data
                     mv /tmp/fishtank.db.fresh /var/lib/docker/volumes/fishtank-dashboard_fishtank-data/_data/fishtank.db
                     chown -R 1000:1000 /var/lib/docker/volumes/fishtank-dashboard_fishtank-data/_data
  5. Start: GIT_COMMIT=\$(git rev-parse --short HEAD) docker compose up -d
  6. Start nginx: systemctl enable --now nginx
EOF
