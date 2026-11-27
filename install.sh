#!/usr/bin/env bash
# KHP Panel — one-command installer for an Ubuntu/Debian server.
# Run as root on the server that will host the panel:
#   REPO_URL=<git url of the khp-panel repo> DOMAIN=<panel domain> bash install.sh
#   e.g. REPO_URL=https://github.com/ahssoe93-wq/khp-panel.git DOMAIN=panel.example.com bash install.sh
# What it does: installs Python 3 + Caddy, pulls the panel code to /opt/khp-panel,
# creates a venv, installs deps, installs + starts the systemd service,
# and configures Caddy to serve https://$DOMAIN with automatic HTTPS.
# It does NOT touch any VPN server or any customer key. After install, open
# https://$DOMAIN, set the one admin password, then add servers in Settings.
set -euo pipefail
REPO_URL="${REPO_URL:-}"
DOMAIN="${DOMAIN:-}"
if [ -z "$REPO_URL" ] || [ -z "$DOMAIN" ]; then
  echo "Usage: REPO_URL=<git-url> DOMAIN=<domain> bash install.sh" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then echo "Run as root." >&2; exit 1; fi

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl debian-keyring debian-archive-keyring apt-transport-https > /dev/null
# Caddy (official repo) for automatic HTTPS
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
apt-get update -qq
apt-get install -y -qq caddy > /dev/null

# Panel code
if [ -d /opt/khp-panel/.git ]; then
  git -C /opt/khp-panel pull --ff-only
else
  rm -rf /opt/khp-panel
  git clone "$REPO_URL" /opt/khp-panel
fi
cd /opt/khp-panel
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# Systemd service (runs gunicorn on 127.0.0.1:5000)
cp khp-panel.service /etc/systemd/system/khp-panel.service
systemctl daemon-reload
systemctl enable --now khp-panel

# Caddy reverse proxy with auto HTTPS
sed "s/DOMAIN_PLACEHOLDER/$DOMAIN/" Caddyfile > /etc/caddy/Caddyfile
systemctl reload caddy

echo
echo "KHP Panel installed. Open https://$DOMAIN and set the admin password."
echo "Then add your servers in Settings (SG-Gift first to test)."
