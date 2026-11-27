# KHP Panel v2

One login, all servers in one place. A custom admin panel for Aung Hein Soe's
Outline VPN business — it talks to his existing 3X-UI and Marzban panels
through their APIs and shows them in one clean KHP-style UI (dark navy +
emerald, mobile-first). It does **not** replace 3X-UI/Marzban; it sits on top
of them.

## What it does (v2)

- **Login** — one admin password, set on first run (hashed, salted).
  The header shows the real KHP shield logo.
- **Dashboard** — servers GROUPED by country (Singapore / Japan / Thailand,
  collapsible) with emoji flags 🇸🇬🇹🇭🇯🇵, green/red status dot from a live
  reachability check (cached ~60 s, auto-refresh every 60 s), client count,
  total used GB, expiring-in-7-days count per server (red), FREE / SALES
  badges, and a "+ Key" quick button per server. A red alert banner appears
  when any server is down ("<name> is down — last seen <time>" + Retry),
  and the "Servers online" stat turns red.
- **Find a key** — global search across ALL servers at once.
- **Server page** — every key: GB used / limit with usage bar, expiry date,
  status (active / expired / disabled / over-limit). Sort chips
  (Expiry / Usage / Name), filter chips (All / Expiring / High usage),
  search within the server, 20 keys per page + "Load more".
  Keys expiring today get a red border.
- **Key actions** — per key: Copy link, **Renew** (+1/+2/+3 mo — the key is
  renamed to the new expiry date, GB unchanged), **Swap** (delete + recreate
  with the same name / expiry / GB but a fresh password), **Disable /
  Enable**, **Edit GB** (400 / 500 / 1000 / 1500), **Delete**. Destructive
  actions (Swap, Delete) need a second tap to confirm.
- **Add Key** — customer name + months (1 / 2 / 3). GB is automatic
  (400 / 1000 / 1500). The key name is made in his convention
  (`Name__D_M_YYYY`, expiry date in the name). After create, the ss:// link
  is shown big with a Copy button. The backend verifies the client exists
  on the panel after creating it.
- **Expiring soon** — keys expiring in the next 7 days, top 5 + "See all"
  to expand; each row has Renew / Swap / Copy buttons.
- **Settings → Servers** — add / edit / remove a server (3X-UI or Marzban),
  with a "Test connection" button per server.

Out of scope for v2: VLESS keys, sales reports, Telegram, reminders,
user roles, inbound management, Xray restart, payments.

## Run (testing)

```bash
cd ~/workspace/khp-panel
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# open http://127.0.0.1:5000 — set the admin password on first run
```

`secret.key` (Fernet key for panel passwords, chmod 600) and
`khp_panel.db` (SQLite) are created automatically on first run.
Both are git-ignored — never commit or share them.

For automated testing only, the env var `KHP_PANEL_ADMIN_PASSWORD` sets the
admin password on first run. Do not use it in production.

## Deploy (later — not done yet)

1. Copy this directory to a server (e.g. `/opt/khp-panel`), create the venv,
   install requirements.
2. Install `khp-panel.service` to `/etc/systemd/system/`, enable + start it
   (gunicorn on 127.0.0.1:5000).
3. Point a domain at the server and use the `Caddyfile` (edit the domain
   first) — Caddy reverse-proxies to gunicorn and issues HTTPS automatically.
4. Open the panel, set the admin password, add servers in Settings.

## How it talks to the panels

- **3X-UI 3.3.1**: session cookie + CSRF token, `POST /login`, then
  `/panel/api/inbounds/list`, `/panel/api/clients/add` (inbound 1, uuid empty,
  `totalGB` in bytes), `/panel/api/clients/del/{email}`,
  `/panel/api/clients/get/{email}` (true `usedTraffic`). Usage shown is the
  MAX of `clientStats` up+down across inbounds — never summed.
- **Marzban 0.8.4**: `POST /api/admin/token` (JWT), `/api/users`,
  `POST /api/user`, `PUT /api/user/{username}` (explicit Shadowsocks
  password — auto-generated passwords are NOT applied to the running core),
  `DELETE /api/user/{username}`. **v2 additions, all verified live:**
  rename via `PUT {"username": new}` (mirrors `tg_marzban_rename.py`),
  disable/enable via `PUT {"status": "disabled"|"active"}`,
  GB change via `PUT {"data_limit": bytes}`, expiry change via
  `PUT {"expire": unix_seconds}`.
- **Links**: `ss://base64(method:password)@share_host:port?type=tcp#name`
  for 3X-UI (Marzban has no `?type=`). Only `method:password` is base64'd,
  no `=` padding, host:port in plain text. Share hosts are the in-server
  domains (never panel domains).
- **Naming/expiry**: `Name__D_M_YYYY` (double underscore, no zero-padding).
  3X-UI expiry = named date + 1 day 00:00 Myanmar (ms). Marzban expiry =
  named date 23:59 Myanmar (unix seconds). Myanmar = UTC+6:30.

## Files

| File | What it is |
| --- | --- |
| `app.py` | Flask app: auth, DB, pages, JSON API |
| `panels.py` | 3X-UI + Marzban clients, link builder, naming/expiry helpers |
| `templates/` | Jinja pages (base, login, setup, dashboard, server, settings) |
| `static/` | `style.css`, `app.js` (plain fetch, no framework), `logo.png` (KHP shield) |
| `requirements.txt` | Flask, requests, cryptography, gunicorn |
| `khp-panel.service` | systemd unit (gunicorn) |
| `Caddyfile` | reverse proxy + auto HTTPS |
| `TEST-REPORT.md` | end-to-end test results (v1 + v2) against the real panels |
