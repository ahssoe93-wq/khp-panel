# KHP Panel — Deployment

## 1. Put the code in a repo (once)

Create a repo for the panel code (suggested: `ahssoe93-wq/khp-panel`,
private is fine) and push this directory to it:

```bash
cd ~/workspace/khp-panel
git init && git add -A && git commit -m "KHP Panel v1"
git remote add origin <repo-url>
git push -u origin main
```

`secret.key` and `khp_panel.db` are git-ignored — no password ever leaves
the server.

## 2. Point the domain at the server (once)

In the domain's DNS, add an A record: the panel domain (e.g.
`panel.khp25.online`) → the host server's public IP. Wait a few minutes
for it to propagate.

## 3. Install on the host server (once)

SSH into the spare server as root and run:

```bash
REPO_URL=<repo-url> DOMAIN=<panel-domain> bash <(curl -Ls <raw-url-of-install.sh>)
```

or copy `install.sh` to the server and run it with those two variables.
It installs Python 3 + Caddy, pulls the code to `/opt/khp-panel`, starts
the `khp-panel` systemd service (gunicorn on 127.0.0.1:5000) and configures
Caddy for automatic HTTPS on the domain.

## 4. First use

Open `https://<panel-domain>` on the phone, set the one admin password,
then Settings → add servers. **Add SG-Gift first and test it**
(create one test key, delete it). Then add PRE8S, TH-XI, PRE7J, and the
rest. The Marzban code path has not been live-tested yet — the first time
a Marzban server (TH-XI / PRE8S / PRE5S) is added, use "Test connection"
and create one throwaway key, then delete it.

## Updating later

```bash
cd /opt/khp-panel && git pull && .venv/bin/pip install -q -r requirements.txt && systemctl restart khp-panel
```

## Notes

- The panel only talks OUT to the servers' panel APIs (3X-UI :2053,
  Marzban :8001). It never touches the Xray core or any customer key
  unless you click Add/Delete in its UI.
- No change-password UI in v1: reset = `rm /opt/khp-panel/khp_panel.db
  /opt/khp-panel/secret.key` + restart the service, then set a new
  password on first open.
