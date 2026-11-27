#!/usr/bin/env python3
"""KHP Panel v1 — one login, all his servers in one place.

Flask + SQLite. Plain HTML/CSS/JS, no build step, mobile-first.
Panel passwords are stored encrypted at rest (Fernet, secret.key chmod 600)
and never logged. Full ss:// links are built only in memory and shown to
the logged-in admin.

Run for testing:  python3 app.py   (port 5000)
Production:       gunicorn (see khp-panel.service + Caddyfile).

First run: open the page, set the admin password on the setup screen.
For automated testing only, KHP_PANEL_ADMIN_PASSWORD env var sets the
admin password on first run — do NOT use it in production.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)

from panels import (GB_BYTES, MM, MONTHS_GB, PanelError, add_months,
                    client_status, expiry_for, fmt_gb, make_client)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "khp_panel.db")
SECRET_KEY_PATH = os.path.join(BASE_DIR, "secret.key")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ---------------------------------------------------------------- secrets
from cryptography.fernet import Fernet  # noqa: E402

if os.path.exists(SECRET_KEY_PATH):
    with open(SECRET_KEY_PATH, "rb") as f:
        secret_key = f.read().strip()
else:
    secret_key = Fernet.generate_key()
    with open(SECRET_KEY_PATH, "wb") as f:
        f.write(secret_key)
    os.chmod(SECRET_KEY_PATH, 0o600)

app.secret_key = secret_key
FERNET = Fernet(secret_key)


def encrypt_pw(plain):
    return FERNET.encrypt(plain.encode()).decode()


def decrypt_pw(enc):
    return FERNET.decrypt(enc.encode()).decode()


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS meta (
                      key TEXT PRIMARY KEY, value TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS servers (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      name TEXT NOT NULL,
                      country TEXT NOT NULL DEFAULT '',
                      type TEXT NOT NULL,
                      base_url TEXT NOT NULL,
                      panel_path TEXT NOT NULL DEFAULT '',
                      admin_user TEXT NOT NULL DEFAULT 'kohein',
                      admin_pw_enc TEXT NOT NULL DEFAULT '',
                      share_host TEXT NOT NULL,
                      share_port INTEGER NOT NULL DEFAULT 1080,
                      is_free INTEGER NOT NULL DEFAULT 0,
                      is_sales INTEGER NOT NULL DEFAULT 0,
                      sort_order INTEGER NOT NULL DEFAULT 0,
                      created_at INTEGER NOT NULL)""")
    db.commit()
    db.close()


init_db()


def meta_get(key):
    row = get_db().execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key, value):
    db = get_db()
    db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))
    db.commit()


def admin_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + ":" + password).encode()).hexdigest()
    return salt, h


def admin_is_set():
    return meta_get("admin_hash") is not None


def admin_check(password):
    stored = meta_get("admin_hash")
    salt = meta_get("admin_salt")
    if not stored or not salt:
        return False
    _, h = admin_hash(password, salt)
    return secrets.compare_digest(h, stored)


def set_admin_password(password):
    salt, h = admin_hash(password)
    meta_set("admin_salt", salt)
    meta_set("admin_hash", h)


# first-run admin password from env (testing convenience only)
_env_pw = os.environ.get("KHP_PANEL_ADMIN_PASSWORD")
if _env_pw:
    with app.app_context():
        if not admin_is_set():
            set_admin_password(_env_pw)


# ---------------------------------------------------------------- auth
def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "login required"}), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


@app.before_request
def require_setup():
    if request.path.startswith("/static/"):
        return
    if not admin_is_set() and request.endpoint not in ("setup",):
        return redirect(url_for("setup"))


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    if not admin_is_set():
        return redirect(url_for("setup"))
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_is_set():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw) < 6:
            error = "Password must be at least 6 characters."
        elif pw != pw2:
            error = "Passwords do not match."
        else:
            set_admin_password(pw)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_is_set():
        return redirect(url_for("setup"))
    error = None
    if request.method == "POST":
        if admin_check(request.form.get("password", "")):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/server/<int:server_id>")
@login_required
def server_page(server_id):
    server = get_server(server_id)
    if not server:
        abort(404)
    return render_template("server.html", server=server)


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


# ---------------------------------------------------------------- servers
def get_server(server_id):
    row = get_db().execute("SELECT * FROM servers WHERE id=?",
                           (server_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["admin_pw"] = decrypt_pw(d["admin_pw_enc"]) if d["admin_pw_enc"] else ""
    return d


def list_servers():
    rows = get_db().execute(
        "SELECT * FROM servers ORDER BY is_sales DESC, sort_order, name"
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d.pop("admin_pw_enc", None)
        out.append(d)
    return out


# health cache: server_id -> (checked_at, online, clients, used_bytes, error)
HEALTH_CACHE = {}
HEALTH_TTL = 60
# last successful check per server (for "last seen" on down servers)
LAST_SEEN = {}


def server_health(server, force=False):
    now = time.time()
    cached = HEALTH_CACHE.get(server["id"])
    if cached and not force and now - cached[0] < HEALTH_TTL:
        return cached[1]
    try:
        client = make_client(server)
        online, count, used, limit = client.health()
        LAST_SEEN[server["id"]] = now
        result = {"online": True, "clients": count, "used": used,
                  "limit": limit, "error": ""}
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        result = {"online": False, "clients": 0, "used": 0, "limit": 0,
                  "error": str(exc)[:200]}
    result["last_seen"] = LAST_SEEN.get(server["id"], 0)
    HEALTH_CACHE[server["id"]] = (now, result)
    return result


def server_clients(server):
    client = make_client(server)
    if server["type"] == "xui":
        return client.list_clients()
    return client.list_users()


def public_client(server_id, c):
    """Client dict safe for JSON (no password, no raw link unless asked)."""
    return {
        "name": c["name"],
        "server_id": server_id,
        "status": c["status"],
        "enable": c.get("enable", True),
        "used": c.get("used", 0),
        "used_h": fmt_gb(c.get("used", 0)),
        "limit": c.get("limit", 0),
        "limit_h": fmt_gb(c["limit"]) if c.get("limit") else "∞",
        "expiry": c.get("expiry") or 0,
        "expiry_h": (datetime.fromtimestamp(c["expiry"], MM).strftime("%d %b %Y")
                     if c.get("expiry") else "never"),
        "pct": (min(100, int(c["used"] * 100 / c["limit"]))
                if c.get("limit") else 0),
    }


# ---------------------------------------------------------------- API
@app.route("/api/servers")
@login_required
def api_servers():
    return jsonify({"ok": True, "servers": list_servers()})


@app.route("/api/health")
@login_required
def api_health():
    force = request.args.get("force") == "1"
    out = []
    for s in list_servers():
        full = get_server(s["id"])
        h = server_health(full, force=force)
        out.append({"id": s["id"], "name": s["name"], **h})
    return jsonify({"ok": True, "health": out,
                    "checked_at": int(time.time())})


@app.route("/api/server/<int:server_id>/clients")
@login_required
def api_clients(server_id):
    server = get_server(server_id)
    if not server:
        return jsonify({"ok": False, "error": "server not found"}), 404
    try:
        clients = server_clients(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    return jsonify({"ok": True, "server": server["name"],
                    "clients": [public_client(server_id, c) for c in clients]})


@app.route("/api/server/<int:server_id>/link/<path:name>")
@login_required
def api_link(server_id, name):
    server = get_server(server_id)
    if not server:
        return jsonify({"ok": False, "error": "server not found"}), 404
    try:
        client = make_client(server)
        if server["type"] == "xui":
            c = client.get_client(name)
        else:
            c = client.get_user(name)
        if not c:
            return jsonify({"ok": False, "error": "client not found"}), 404
        link = client.build_link(c)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    return jsonify({"ok": True, "name": name, "link": link})


@app.route("/api/server/<int:server_id>/add-key", methods=["POST"])
@login_required
def api_add_key(server_id):
    server = get_server(server_id)
    if not server:
        return jsonify({"ok": False, "error": "server not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    months = int(data.get("months") or 1)
    if months not in MONTHS_GB:
        return jsonify({"ok": False, "error": "months must be 1, 2 or 3"}), 400
    gb = MONTHS_GB[months]
    total_bytes = GB_BYTES[gb]
    # explicit full name override (testing / advanced); otherwise build from
    # the customer name + the expiry date in his convention.
    full_name = (data.get("client_name") or "").strip()
    if full_name:
        name = full_name
        exp_date, _suffix = expiry_for(months)
    else:
        raw = (data.get("name") or "").strip()
        raw = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if not raw:
            return jsonify({"ok": False, "error": "customer name needed"}), 400
        exp_date, suffix = expiry_for(months)
        name = "%s__%s" % (raw, suffix)
    try:
        client = make_client(server)
        if server["type"] == "xui":
            exp_ms = int((datetime(exp_date.year, exp_date.month, exp_date.day,
                                   tzinfo=MM) + timedelta(days=1)).timestamp() * 1000)
            created = client.create_client(name, total_bytes, exp_ms)
        else:
            exp_s = int(datetime(exp_date.year, exp_date.month, exp_date.day,
                                 23, 59, tzinfo=MM).timestamp())
            created = client.create_user(name, total_bytes, exp_s)
        link = client.build_link(created)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    return jsonify({"ok": True, "name": name, "link": link,
                    "expiry": exp_date.isoformat(), "gb": gb,
                    "months": months})


@app.route("/api/server/<int:server_id>/delete-key", methods=["POST"])
@login_required
def api_delete_key(server_id):
    server = get_server(server_id)
    if not server:
        return jsonify({"ok": False, "error": "server not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "client name needed"}), 400
    try:
        client = make_client(server)
        if server["type"] == "xui":
            client.delete_client(name)
        else:
            client.delete_user(name)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "name": name, "gone": True})


# ---------------------------------------------------------------- key actions (v2)
def _key_action_server(server_id):
    server = get_server(server_id)
    if not server:
        return None, (jsonify({"ok": False, "error": "server not found"}), 404)
    return server, None


@app.route("/api/server/<int:server_id>/clients/<path:name>/renew",
           methods=["POST"])
@login_required
def api_renew_key(server_id, name):
    """Renew +1/+2/+3 months: new expiry = old expiry + 30/60/90 days.
    The key is RENAMED to the new date (the name encodes the expiry) and
    the expiry moves in the same update call. GB is unchanged."""
    server, err = _key_action_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    months = int(data.get("months") or 1)
    if months not in (1, 2, 3):
        return jsonify({"ok": False, "error": "months must be 1, 2 or 3"}), 400
    try:
        client = make_client(server)
        if server["type"] == "xui":
            rec, new_name = client.renew_client(name, months)
        else:
            rec, new_name = client.renew_user(name, months)
        link = client.build_link(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "old_name": name, "name": new_name,
                    "link": link,
                    "expiry": rec.get("expiry") or 0,
                    "expiry_h": (datetime.fromtimestamp(rec["expiry"], MM)
                                 .strftime("%d %b %Y") if rec.get("expiry")
                                 else "never"),
                    "gb": int((rec.get("limit") or 0) // 1073741824),
                    "months": months})


@app.route("/api/server/<int:server_id>/clients/<path:name>/swap",
           methods=["POST"])
@login_required
def api_swap_key(server_id, name):
    """Swap: DELETE the old client first, then create a fresh one with the
    SAME name / expiry / GB but a fresh password. Used when a key does not
    connect."""
    server, err = _key_action_server(server_id)
    if err:
        return err
    try:
        client = make_client(server)
        if server["type"] == "xui":
            rec = client.swap_client(name)
        else:
            rec = client.swap_user(name)
        link = client.build_link(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "name": name, "link": link,
                    "expiry": rec.get("expiry") or 0})


@app.route("/api/server/<int:server_id>/clients/<path:name>/toggle",
           methods=["POST"])
@login_required
def api_toggle_key(server_id, name):
    """Disable / enable a key. Body: {"enable": true/false}."""
    server, err = _key_action_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    enable = bool(data.get("enable"))
    try:
        client = make_client(server)
        if server["type"] == "xui":
            rec = client.toggle_client(name, enable)
        else:
            rec = client.toggle_user(name, enable)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "name": name, "enable": rec.get("enable"),
                    "status": rec.get("status")})


@app.route("/api/server/<int:server_id>/clients/<path:name>/gb",
           methods=["POST"])
@login_required
def api_gb_key(server_id, name):
    """Change a key's GB limit. Body: {"gb": 400|500|1000|1500}."""
    server, err = _key_action_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    try:
        gb = int(data.get("gb") or 0)
    except (TypeError, ValueError):
        gb = 0
    if gb not in GB_BYTES:
        return jsonify({"ok": False,
                        "error": "gb must be one of 400, 500, 1000, 1500"}), 400
    try:
        client = make_client(server)
        if server["type"] == "xui":
            rec = client.set_gb_client(name, GB_BYTES[gb])
        else:
            rec = client.set_gb_user(name, GB_BYTES[gb])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "name": name, "gb": gb,
                    "limit": rec.get("limit")})


@app.route("/api/server/<int:server_id>/clients/<path:name>/edit",
           methods=["POST"])
@login_required
def api_edit_key(server_id, name):
    """Edit a key (v3): custom GB limit and/or expiry date. Body:
    {"gb": <GB number, optional>, "expiry_date": "YYYY-MM-DD", optional}.
    A date change RENAMES the key to the new date (the name encodes the
    expiry); a GB-only change does not rename. Returns the new name +
    new link, like the renew endpoint."""
    server, err = _key_action_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    gb = None
    if data.get("gb") is not None:
        try:
            gb = float(data.get("gb"))
        except (TypeError, ValueError):
            gb = 0
        if not gb or gb <= 0 or gb > 100000:
            return jsonify({"ok": False,
                            "error": "gb must be a positive number of GB"}), 400
    new_date = None
    if data.get("expiry_date"):
        try:
            new_date = datetime.strptime(
                str(data["expiry_date"]).strip(), "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False,
                            "error": "expiry_date must be YYYY-MM-DD"}), 400
    if gb is None and new_date is None:
        return jsonify({"ok": False, "error": "nothing to change"}), 400
    try:
        client = make_client(server)
        gb_bytes = int(gb * 1073741824) if gb is not None else None
        if server["type"] == "xui":
            rec, new_name = client.edit_client(name, gb_bytes, new_date)
        else:
            rec, new_name = client.edit_user(name, gb_bytes, new_date)
        link = client.build_link(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "old_name": name, "name": new_name,
                    "link": link,
                    "expiry": rec.get("expiry") or 0,
                    "expiry_h": (datetime.fromtimestamp(rec["expiry"], MM)
                                 .strftime("%d %b %Y") if rec.get("expiry")
                                 else "never"),
                    "gb": int((rec.get("limit") or 0) // 1073741824)})


@app.route("/api/search")
@login_required
def api_search():
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify({"ok": True, "results": []})
    results = []
    for s in list_servers():
        full = get_server(s["id"])
        try:
            clients = server_clients(full)
        except Exception:  # noqa: BLE001 - one down server must not break search
            continue
        for c in clients:
            if q in c["name"].lower():
                pc = public_client(s["id"], c)
                pc["server_name"] = s["name"]
                pc["server_type"] = s["type"]
                results.append(pc)
    results.sort(key=lambda r: r["name"].lower())
    return jsonify({"ok": True, "results": results[:100]})


@app.route("/api/expiring")
@login_required
def api_expiring():
    days = int(request.args.get("days") or 7)
    soon = time.time() + days * 86400
    now = time.time()
    results = []
    for s in list_servers():
        full = get_server(s["id"])
        try:
            clients = server_clients(full)
        except Exception:  # noqa: BLE001
            continue
        for c in clients:
            exp = c.get("expiry") or 0
            if exp and now <= exp <= soon:
                pc = public_client(s["id"], c)
                pc["server_name"] = s["name"]
                pc["days_left"] = max(0, int((exp - now) // 86400))
                results.append(pc)
    results.sort(key=lambda r: r["expiry"])
    return jsonify({"ok": True, "results": results[:100]})


@app.route("/api/server/<int:server_id>/test", methods=["POST"])
@login_required
def api_test(server_id):
    server = get_server(server_id)
    if not server:
        return jsonify({"ok": False, "error": "server not found"}), 404
    try:
        client = make_client(server)
        online, count, used, _limit = client.health()
        return jsonify({"ok": True, "online": True, "clients": count,
                        "used_h": fmt_gb(used)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "online": False,
                        "error": str(exc)[:300]}), 502


@app.route("/api/servers", methods=["POST"])
@login_required
def api_add_server():
    data = request.get_json(force=True, silent=True) or {}
    for field in ("name", "type", "base_url", "share_host"):
        if not (data.get(field) or "").strip():
            return jsonify({"ok": False, "error": "%s is required" % field}), 400
    if data["type"] not in ("xui", "marzban"):
        return jsonify({"ok": False, "error": "type must be xui or marzban"}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO servers (name, country, type, base_url, panel_path,
           admin_user, admin_pw_enc, share_host, share_port, is_free,
           is_sales, sort_order, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data["name"].strip(), (data.get("country") or "").strip(),
         data["type"], data["base_url"].strip().rstrip("/"),
         (data.get("panel_path") or "").strip(),
         (data.get("admin_user") or "kohein").strip(),
         encrypt_pw(data.get("admin_pw") or ""),
         data["share_host"].strip(), int(data.get("share_port") or 1080),
         1 if data.get("is_free") else 0, 1 if data.get("is_sales") else 0,
         int(data.get("sort_order") or 0), int(time.time())))
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/servers/<int:server_id>", methods=["PUT"])
@login_required
def api_edit_server(server_id):
    if not get_server(server_id):
        return jsonify({"ok": False, "error": "server not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    fields, values = [], []
    for col in ("name", "country", "type", "base_url", "panel_path",
                "admin_user", "share_host", "share_port", "is_free",
                "is_sales", "sort_order"):
        if col in data:
            fields.append("%s=?" % col)
            values.append(data[col])
    if "admin_pw" in data and data["admin_pw"]:
        fields.append("admin_pw_enc=?")
        values.append(encrypt_pw(data["admin_pw"]))
    if not fields:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    values.append(server_id)
    db.execute("UPDATE servers SET %s WHERE id=?" % ", ".join(fields), values)
    db.commit()
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True})


@app.route("/api/servers/<int:server_id>", methods=["DELETE"])
@login_required
def api_delete_server(server_id):
    db = get_db()
    db.execute("DELETE FROM servers WHERE id=?", (server_id,))
    db.commit()
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
