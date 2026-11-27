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
# The venv needs the ensurepip module for the EXACT python3 version on this
# box (Ubuntu 20.04 default is 3.8, but this server's python3 is 3.9 — the
# plain python3-venv package installs the 3.8 one and the venv build fails).
PYV="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
apt-get install -y -qq "python${PYV}-venv" > /dev/null || echo "NOTE: python${PYV}-venv not found in apt; venv creation may fail" >&2
# Caddy (official repo) for automatic HTTPS
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
apt-get update -qq
apt-get install -y -qq caddy > /dev/null

# Panel code
# NOTE: /opt/khp-panel ends up owned by www-data (the service user, see the
# chown below), so root's git refuses to work in it ("fatal: detected dubious
# ownership") unless the directory is marked safe for this command.
if [ -d /opt/khp-panel/.git ]; then
  git -c safe.directory=/opt/khp-panel -C /opt/khp-panel pull --ff-only
else
  rm -rf /opt/khp-panel
  git clone "$REPO_URL" /opt/khp-panel
fi
cd /opt/khp-panel
# Clean slate for re-runs: a failed earlier run leaves a broken half-made venv.
rm -rf .venv
# Pick a Python with a working venv: prefer the stock Ubuntu 20.04 python3.8
# (its venv module comes with the python3-venv package installed above);
# otherwise use the default python3.
if command -v python3.8 >/dev/null 2>&1; then PYBIN=python3.8; else PYBIN=python3; fi
# Normal venv first; if ensurepip is missing (custom-built python3.9), retry
# without pip and fetch pip directly from bootstrap.pypa.io instead.
if ! "$PYBIN" -m venv .venv >/dev/null 2>&1; then
  echo "venv with pip failed (ensurepip missing) — retrying without pip..."
  rm -rf .venv
  "$PYBIN" -m venv --without-pip .venv
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  .venv/bin/python /tmp/get-pip.py --quiet
fi
.venv/bin/pip install -q -r requirements.txt
# The service runs as www-data, so it must own the app dir (it writes the
# SQLite DB + secret.key there at runtime).
chown -R www-data:www-data /opt/khp-panel

# Systemd service (runs gunicorn on 127.0.0.1:5000)
cp khp-panel.service /etc/systemd/system/khp-panel.service
systemctl daemon-reload
systemctl enable khp-panel
# Restart (not just start) so a re-run picks up freshly pulled code.
# Sessions survive the restart: Flask sessions are signed cookies and
# secret.key is untouched by git pull, and the SQLite DB is untracked.
systemctl restart khp-panel

# Caddy reverse proxy with auto HTTPS
sed "s/DOMAIN_PLACEHOLDER/$DOMAIN/" Caddyfile > /etc/caddy/Caddyfile
systemctl reload caddy

echo
echo "KHP Panel installed. Open https://$DOMAIN and set the admin password."
echo "Then add your servers in Settings (SG-Gift first to test)."
