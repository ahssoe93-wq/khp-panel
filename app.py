#!/usr/bin/env python3
"""KHP Panel v6 — one login, all his servers in one place.

Flask + SQLite. Plain HTML/CSS/JS, no build step, mobile-first.
Panel passwords are stored encrypted at rest (Fernet, secret.key chmod 600)
and never logged. Full ss:// / vless:// links are built only in memory
and shown to the logged-in user.

v6: MULTI-USER — the Super Admin (admin) sees/manages ALL servers;
resellers log in with username + password and see/manage ONLY their own
servers (servers.owner). Ownership is enforced server-side in every
route. VLESS (3X-UI only) is managed alongside Outline. The legacy
single-admin hash in meta is migrated into the users table on startup
(the admin password is NEVER reset by the migration).

Run for testing:  python3 app.py   (port 5000)
Production:       gunicorn (see khp-panel.service + Caddyfile).

First run: open the page, set the admin password on the setup screen.
For automated testing only, KHP_PANEL_ADMIN_PASSWORD env var sets the
admin password on first run — do NOT use it in production.
"""
import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, session, url_for)

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
    # v6: servers.owner — 'admin' (his own) or a reseller username.
    cols = [r[1] for r in db.execute("PRAGMA table_info(servers)")]
    if "owner" not in cols:
        db.execute("ALTER TABLE servers ADD COLUMN owner TEXT "
                   "NOT NULL DEFAULT 'admin'")
    # v6: users — username PK, pw_hash/pw_salt (same scheme as the legacy
    # admin hash), role admin/reseller, display_name (header branding).
    db.execute("""CREATE TABLE IF NOT EXISTS users (
                      username TEXT PRIMARY KEY,
                      pw_hash TEXT NOT NULL,
                      pw_salt TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'reseller',
                      display_name TEXT NOT NULL DEFAULT '',
                      created_at INTEGER NOT NULL)""")
    # v6.1.6: per-user, per-protocol price/GB presets — one row per
    # tier. (username, protocol, months, gb) is the key on purpose:
    # MK's VLESS line has THREE 1M tiers (100/150/200 GB), so months
    # alone cannot be the key. price_ks is display-only.
    db.execute("""CREATE TABLE IF NOT EXISTS user_presets (
                      username TEXT NOT NULL,
                      protocol TEXT NOT NULL,
                      months INTEGER NOT NULL,
                      gb INTEGER NOT NULL,
                      price_ks INTEGER NOT NULL DEFAULT 0,
                      PRIMARY KEY (username, protocol, months, gb))""")
    # v6.1.6: SBH's channel name was misspelled SHILED — fix the
    # stored display name (user decision 2026-11-28).
    db.execute("UPDATE users SET display_name='SBH - NET SHIELD' "
               "WHERE display_name='SBH - NET SHILED'")
    # v4: keys created through THIS panel, so the server page can sort
    # "Newest" first and show a NEW badge. Keys created elsewhere (or
    # before v4) have no row here and sort after, by expiry.
    db.execute("""CREATE TABLE IF NOT EXISTS panel_keys (
                      server_id INTEGER NOT NULL,
                      name TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      PRIMARY KEY (server_id, name))""")
    db.commit()
    migrate_legacy_admin(db)
    db.close()


def migrate_legacy_admin(db):
    """v6 startup migration: the old single-admin hash lived in meta
    (admin_hash/admin_salt). If the users table has NO admin yet and the
    legacy hash exists, move it into users as the admin row — the
    password is NEVER reset and the legacy rows are left in place.
    Runs at import (init_db) and is safe to call again."""
    row = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'"
                     ).fetchone()
    if row and row[0]:
        return
    legacy = db.execute(
        "SELECT key, value FROM meta WHERE key IN ('admin_hash', "
        "'admin_salt')").fetchall()
    vals = {r[0]: r[1] for r in legacy}
    if vals.get("admin_hash") and vals.get("admin_salt"):
        db.execute(
            "INSERT OR IGNORE INTO users (username, pw_hash, pw_salt, "
            "role, display_name, created_at) VALUES (?, ?, ?, 'admin', "
            "'KHP PANEL', ?)",
            ("admin", vals["admin_hash"], vals["admin_salt"],
             int(time.time())))
        db.commit()


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


# ---------------------------------------------------------------- panel_keys (v4)
def note_key_created(server_id, name, created_at=None):
    db = get_db()
    db.execute("INSERT INTO panel_keys(server_id, name, created_at) "
               "VALUES(?, ?, ?) ON CONFLICT(server_id, name) "
               "DO UPDATE SET created_at=excluded.created_at",
               (server_id, name, int(created_at or time.time())))
    db.commit()


def note_key_deleted(server_id, name):
    db = get_db()
    db.execute("DELETE FROM panel_keys WHERE server_id=? AND name=?",
               (server_id, name))
    db.commit()


def note_key_renamed(server_id, old_name, new_name):
    """Keep the original created_at when a key is renamed (renew / edit)."""
    db = get_db()
    row = db.execute("SELECT created_at FROM panel_keys "
                     "WHERE server_id=? AND name=?",
                     (server_id, old_name)).fetchone()
    if row:
        db.execute("DELETE FROM panel_keys WHERE server_id=? AND name=?",
                   (server_id, old_name))
        # v4: upsert — the target name may already have a row (a previous
        # key with the same name, or a direct panel delete that bypassed
        # note_key_deleted); a plain INSERT would 500 the request even
        # though the panel rename already succeeded.
        db.execute("INSERT INTO panel_keys(server_id, name, created_at) "
                   "VALUES(?, ?, ?) ON CONFLICT(server_id, name) "
                   "DO UPDATE SET created_at=excluded.created_at",
                   (server_id, new_name, row[0]))
        db.commit()


def created_map(server_id):
    """name -> created_at (unix) for keys this panel created."""
    rows = get_db().execute("SELECT name, created_at FROM panel_keys "
                            "WHERE server_id=?", (server_id,)).fetchall()
    return {r["name"]: r["created_at"] for r in rows}


def user_hash(password, salt=None):
    """Same scheme as the legacy admin hash: sha256(salt + ':' + pw)."""
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + ":" + password).encode()).hexdigest()
    return salt, h


def admin_hash(password, salt=None):
    # Back-compat alias — the legacy meta hash used this name.
    return user_hash(password, salt)


def get_user(username):
    row = get_db().execute(
        "SELECT username, pw_hash, pw_salt, role, display_name, "
        "created_at FROM users WHERE username=?",
        (username,)).fetchone()
    return dict(row) if row else None


def list_users():
    rows = get_db().execute(
        "SELECT username, role, display_name, created_at FROM users "
        "ORDER BY role DESC, username").fetchall()
    return [dict(r) for r in rows]


def create_user(username, password, role, display_name):
    salt, h = user_hash(password)
    db = get_db()
    db.execute(
        "INSERT INTO users (username, pw_hash, pw_salt, role, "
        "display_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (username, h, salt, role, display_name, int(time.time())))
    db.commit()


def set_user_password(username, password):
    salt, h = user_hash(password)
    db = get_db()
    db.execute("UPDATE users SET pw_hash=?, pw_salt=? WHERE username=?",
               (h, salt, username))
    db.commit()


def set_user_display_name(username, display_name):
    db = get_db()
    db.execute("UPDATE users SET display_name=? WHERE username=?",
               (display_name, username))
    db.commit()


def delete_user(username):
    db = get_db()
    db.execute("DELETE FROM users WHERE username=?", (username,))
    db.execute("DELETE FROM user_presets WHERE username=?", (username,))
    db.commit()


# ---------------------------------------------------------------- v6.1.6: per-user price/GB presets
# Seed values (months, GB, price in Ks) — the user's own tiers plus
# the six resellers' tiers as given 2026-11-28. "vless": None means
# "copy the Outline tiers" (only MK sells VLESS on different tiers).
DEFAULT_PRESETS = {
    "admin": {"outline": [(1, 400, 4000), (2, 1000, 7000),
                          (3, 1500, 10000)], "vless": None},
    "sth28": {"outline": [(1, 200, 5000), (2, 600, 10000),
                          (3, 1000, 15000)], "vless": None},
    "sbh04": {"outline": [(1, 300, 5000), (2, 700, 10000),
                          (3, 1100, 15000), (4, 1500, 20000),
                          (5, 1900, 25000), (6, 2300, 30000)],
              "vless": None},
    "www93": {"outline": [(1, 250, 5000), (2, 500, 9500),
                          (3, 750, 13500), (6, 1500, 25000)],
              "vless": None},
    "xxx09": {"outline": [(1, 300, 5000), (2, 700, 9000),
                          (3, 1100, 13000), (4, 1500, 17000)],
              "vless": None},
    "mt95": {"outline": [(1, 300, 6000)], "vless": None},
    "mk10": {"outline": [(1, 300, 5000), (2, 700, 9500),
                         (3, 1100, 14000), (4, 1500, 18000)],
             "vless": [(1, 100, 4000), (1, 150, 5000), (1, 200, 6000)]},
}
# A user nobody seeded yet (e.g. a reseller added later) gets the
# admin's tiers — the admin can change them in Settings → Users.
GENERIC_PRESETS = DEFAULT_PRESETS["admin"]


def _seed_user_presets(db, username):
    """Insert the seed tiers for a user that has no preset rows yet.
    VLESS seeds copy the Outline tiers unless the seed says otherwise.
    Idempotent: a user with ANY preset row is left alone, so admin
    edits survive reinstalls (the DB is preserved)."""
    have = db.execute(
        "SELECT COUNT(*) FROM user_presets WHERE username=?",
        (username,)).fetchone()[0]
    if have:
        return
    seed = DEFAULT_PRESETS.get(username) or GENERIC_PRESETS
    for protocol in ("outline", "vless"):
        tiers = seed.get(protocol)
        if tiers is None:
            tiers = seed["outline"]
        for months, gb, price in tiers:
            db.execute(
                "INSERT OR IGNORE INTO user_presets "
                "(username, protocol, months, gb, price_ks) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, protocol, months, gb, price))


def ensure_user_presets(username):
    db = get_db()
    _seed_user_presets(db, username)
    db.commit()


def get_user_presets(username):
    """{protocol: [{months, gb, price}]} sorted by months, then GB.
    Lazily seeds the defaults on first read."""
    ensure_user_presets(username)
    rows = get_db().execute(
        "SELECT protocol, months, gb, price_ks FROM user_presets "
        "WHERE username=? ORDER BY protocol, months, gb",
        (username,)).fetchall()
    out = {"outline": [], "vless": []}
    for r in rows:
        out[r["protocol"]].append({"months": r["months"], "gb": r["gb"],
                                   "price": r["price_ks"]})
    return out


def validate_preset_tiers(tiers):
    """A protocol's tier list: 1..6 tiers, months 1..12, GB > 0,
    price >= 0, no duplicate (months, GB) pair. Returns the cleaned
    list of (months, gb, price) tuples or raises ValueError."""
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 6:
        raise ValueError("each protocol needs 1..6 tiers")
    seen, clean = set(), []
    for t in tiers:
        if not isinstance(t, dict):
            raise ValueError("each tier needs months, gb and price")
        try:
            months = int(t["months"])
            gb = int(t["gb"])
            price = int(t["price"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("each tier needs months, gb and price")
        if not 1 <= months <= 12:
            raise ValueError("months must be 1..12")
        if gb <= 0 or gb > 100000:
            raise ValueError("gb must be a positive number")
        if price < 0 or price > 100000000:
            raise ValueError("price must be 0 or more")
        if (months, gb) in seen:
            raise ValueError("duplicate tier (months + GB)")
        seen.add((months, gb))
        clean.append((months, gb, price))
    clean.sort(key=lambda x: (x[0], x[1]))
    return clean


def set_user_presets(username, outline, vless):
    """Replace one or both protocols' tiers for a user. A protocol
    passed as None is left unchanged."""
    db = get_db()
    for protocol, tiers in (("outline", outline), ("vless", vless)):
        if tiers is None:
            continue
        clean = validate_preset_tiers(tiers)
        db.execute("DELETE FROM user_presets WHERE username=? "
                   "AND protocol=?", (username, protocol))
        for months, gb, price in clean:
            db.execute(
                "INSERT INTO user_presets (username, protocol, months, "
                "gb, price_ks) VALUES (?, ?, ?, ?, ?)",
                (username, protocol, months, gb, price))
    db.commit()


def resolve_preset_gb(username, protocol, months, gb=None):
    """Resolve the GB for a key create. Returns (gb, is_preset).
    gb is the tier's GB when (months, gb) matches a preset tier, or
    the single tier for that month; a GB that matches no tier is a
    custom GB (is_preset False); None when the month has no tier and
    no GB was given (ambiguous or missing — e.g. MK's three 1M VLESS
    tiers with months alone)."""
    tiers = get_user_presets(username).get(protocol, [])
    if gb is not None:
        gb = int(gb) if float(gb).is_integer() else gb
        for t in tiers:
            if t["months"] == months and t["gb"] == gb:
                return gb, True
        return gb, False
    matches = [t for t in tiers if t["months"] == months]
    if len(matches) == 1:
        return matches[0]["gb"], True
    return None, False


def user_check(username, password):
    """Verify a username + password. Returns the user dict or None —
    callers must NOT distinguish 'unknown user' from 'wrong password'."""
    u = get_user(username)
    if not u:
        return None
    _, h = user_hash(password, u["pw_salt"])
    if not secrets.compare_digest(h, u["pw_hash"]):
        return None
    return u


def admin_is_set():
    """An admin user exists (migrated legacy hash counts — the
    migration runs at startup in init_db)."""
    row = get_db().execute(
        "SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()
    return bool(row and row[0])


def admin_check(password):
    """Legacy helper: check the admin password. Kept for compat."""
    return bool(user_check("admin", password))


def set_admin_password(password):
    """Legacy helper: set the admin password (creates the admin user
    if missing, otherwise resets it)."""
    if get_user("admin"):
        set_user_password("admin", password)
    else:
        create_user("admin", password, "admin", "KHP PANEL")


# first-run admin password from env (testing convenience only)
_env_pw = os.environ.get("KHP_PANEL_ADMIN_PASSWORD")
if _env_pw:
    with app.app_context():
        if not admin_is_set():
            set_admin_password(_env_pw)


# ---------------------------------------------------------------- auth
def current_user():
    """The logged-in user from the session (admin when nothing is set,
    e.g. the legacy single-admin session)."""
    return {"username": session.get("username") or "admin",
            "role": session.get("role") or "admin",
            "display_name": session.get("display_name") or "KHP PANEL"}


def owner_key():
    """Cache/ownership key for the current user: 'admin' for the Super
    Admin, the reseller username otherwise."""
    u = current_user()
    return "admin" if u["role"] == "admin" else u["username"]


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


def require_admin(fn):
    """Super Admin only. API routes get a 403 JSON, pages get a 403."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user()["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False,
                                "error": "admin only"}), 403
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def _set_session(user):
    session["logged_in"] = True
    session["username"] = user["username"]
    session["role"] = user["role"]
    session["display_name"] = user["display_name"] or user["username"]


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
            # v6: first run creates the Super Admin user (username admin).
            create_user("admin", pw, "admin", "KHP PANEL")
            _set_session(get_user("admin"))
            return redirect(url_for("dashboard"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_is_set():
        return redirect(url_for("setup"))
    error = None
    if request.method == "POST":
        # v6: username + password; one generic message for both an
        # unknown user and a wrong password (never say which).
        user = user_check(request.form.get("username", "").strip(),
                          request.form.get("password", ""))
        if user:
            _set_session(user)
            return redirect(url_for("dashboard"))
        error = "Wrong username or password."
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
    server = visible_server(server_id)
    if server is None:
        if get_server(server_id):
            abort(403)
        abort(404)
    return render_template("server.html", server=server)


@app.route("/settings")
@login_required
@require_admin
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
    """Servers the CURRENT user may see: ALL for the Super Admin,
    only owner == username for a reseller. Ownership is enforced HERE
    (server-side), never by hiding rows in the UI."""
    u = current_user()
    if u["role"] == "admin":
        rows = get_db().execute(
            "SELECT * FROM servers ORDER BY is_sales DESC, sort_order, name"
        ).fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM servers WHERE owner=? "
            "ORDER BY is_sales DESC, sort_order, name",
            (u["username"],)).fetchall()
    # v6.1: attach the owner's channel display name (resellers see their
    # channel name in the admin's owner-grouped dashboard).
    users = {r["username"]: (r["display_name"] or r["username"])
             for r in get_db().execute(
                 "SELECT username, display_name FROM users").fetchall()}
    out = []
    for row in rows:
        d = dict(row)
        d.pop("admin_pw_enc", None)
        d["owner_display"] = users.get(d.get("owner") or "admin",
                                       d.get("owner") or "admin")
        out.append(d)
    return out


def visible_server(server_id):
    """The server row if the current user may see it, else None."""
    s = get_server(server_id)
    if not s:
        return None
    u = current_user()
    if u["role"] != "admin" and s.get("owner") != u["username"]:
        return None
    return s


def _owned_server(server_id):
    """(server, error_response) for API routes: 404 when the server
    does not exist, 403 JSON when it belongs to someone else."""
    s = get_server(server_id)
    if not s:
        return None, (jsonify({"ok": False, "error": "server not found"}), 404)
    u = current_user()
    if u["role"] != "admin" and s.get("owner") != u["username"]:
        return None, (jsonify({"ok": False, "error": "not your server"}), 403)
    return s, None


# health cache: server_id -> (checked_at, online, clients, used_bytes, error)
HEALTH_CACHE = {}
HEALTH_TTL = 60
# expiring scan cache: owner_key -> [checked_at, results] — the scan hits
# all visible panels, so a 60s cache keeps the dashboard + auto-refresh
# from re-scanning. Keyed per owner: a reseller must NEVER see another
# owner's cached expiring keys.
EXPIRING_CACHE = {}
# v6.1: key-overview cache: owner_key -> [checked_at, payload] — same
# one-pass scan, same 60s TTL, same per-owner keying.
OVERVIEW_CACHE = {}
# last successful check per server (for "last seen" on down servers)
LAST_SEEN = {}


def invalidate_overview():
    """v6.1.2: a delete changes the dashboard's key-overview scan, so
    drop the cached payloads — otherwise the next quiet refresh (the
    server cache lives 60s) would resurrect the deleted rows in the
    dashboard sections and undo the client's in-place patch. The next
    fetch re-scans the panels."""
    OVERVIEW_CACHE.clear()
    EXPIRING_CACHE.clear()


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


def server_all_clients(server):
    """Outline + VLESS clients (3X-UI) or Marzban users, normalised.
    v6.1.5: 3X-UI returns ONE record per customer (email) — a dual
    Outline+VLESS customer is protocol='both' with both sub-records;
    Outline-only / VLESS-only stay protocol='outline' / 'vless'."""
    client = make_client(server)
    if server["type"] == "xui":
        return client.list_merged_clients()
    return client.list_users()


def public_client(server_id, c, created=None):
    """Client dict safe for JSON (no password, no raw link unless asked)."""
    d = {
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
        # v4: unix time this panel created the key (0 = created elsewhere
        # or before v4); drives the "Newest" sort and the NEW badge.
        "created_at": created or 0,
        # v6: which protocol this key is (outline / vless / both).
        # v6.1.5: "both" = one customer holding an Outline key AND a
        # VLESS key; "protocols" lists the sides present.
        "protocol": c.get("protocol", "outline"),
        "protocols": c.get("protocols",
                           [c.get("protocol", "outline")]),
    }
    # v6.1: online is included ONLY when the panel actually supplied it
    # (3X-UI onlines endpoint, or Marzban online_at). Absent = unknown,
    # and the UI omits the pill instead of showing Offline.
    if isinstance(c.get("online"), bool):
        d["online"] = c["online"]
    if c.get("uuid"):
        d["uuid"] = c["uuid"]
    return d


# ---------------------------------------------------------------- API
@app.route("/api/servers")
@login_required
def api_servers():
    # v6.1: the dashboard needs the role to pick owner-grouping (admin)
    # vs country-grouping (reseller).
    return jsonify({"ok": True, "role": current_user()["role"],
                    "servers": list_servers()})


@app.route("/api/health")
@login_required
def api_health():
    # v4: check the 12 servers in PARALLEL, not one by one — a cold
    # check used to take the sum of 12 panel round-trips. The full
    # server rows are read in this thread first (get_db needs the Flask
    # request context); only the network checks run in the pool.
    force = request.args.get("force") == "1"
    servers = [get_server(s["id"]) for s in list_servers()]
    out = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for full, h in zip(servers, pool.map(
                lambda s: server_health(s, force=force), servers)):
            out.append({"id": full["id"], "name": full["name"], **h})
    return jsonify({"ok": True, "health": out,
                    "checked_at": int(time.time())})


@app.route("/api/server/<int:server_id>/clients")
@login_required
def api_clients(server_id):
    server, err = _owned_server(server_id)
    if err:
        return err
    try:
        client = make_client(server)
        if server["type"] == "xui":
            # v6.1.5: ONE row per customer (email) — dual Outline+VLESS
            # customers come back as a single protocol='both' record
            # carrying both links; VLESS-only as protocol='vless'.
            clients = client.list_merged_clients()
            # v6.1: one extra POST for the currently-connected list; a
            # failed read returns None and the pill is omitted (never
            # "everyone Offline"). Marzban users already carry "online"
            # from list_users (online_at recency).
            online = client.online_emails()
            if online is not None:
                for c in clients:
                    c["online"] = c["name"] in online
            # v6: tell the page which VLESS inbounds exist (network +
            # port only — no credentials), so it can show the VLESS
            # section and the Outline/VLESS switch in the Add Key modal.
            vless_info = [{"network": v["network"], "port": v["port"]}
                          for v in client.detect_inbounds()["vless"]]
        else:
            clients = client.list_users()
            vless_info = []
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    created = created_map(server_id)
    return jsonify({"ok": True, "server": server["name"],
                    "vless": vless_info,
                    "clients": [public_client(server_id, c,
                                              created.get(c["name"]))
                                for c in clients]})


@app.route("/api/server/<int:server_id>/link/<path:name>")
@login_required
def api_link(server_id, name):
    server, err = _owned_server(server_id)
    if err:
        return err
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
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    # v4: optional custom GB and custom expiry date override the preset.
    custom_gb = None
    if data.get("gb") is not None:
        try:
            custom_gb = float(data.get("gb"))
        except (TypeError, ValueError):
            custom_gb = 0
        if not custom_gb or custom_gb <= 0 or custom_gb > 100000:
            return jsonify({"ok": False,
                            "error": "gb must be a positive number"}), 400
    custom_date = None
    if data.get("expiry_date"):
        try:
            custom_date = datetime.strptime(
                str(data["expiry_date"]).strip(), "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False,
                            "error": "expiry_date must be YYYY-MM-DD"}), 400
    try:
        months = int(data.get("months") or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 12:
        return jsonify({"ok": False, "error": "months must be 1..12"}), 400
    # v6.1.6: the GB comes from the caller's own preset tier for this
    # protocol; a GB matching no tier is a custom GB. months with no
    # tier and no GB (e.g. MK's three 1M VLESS tiers, months alone)
    # cannot resolve and must pick a tier / enter a GB.
    gb, is_preset = resolve_preset_gb(current_user()["username"],
                                      "outline", months, custom_gb)
    if gb is None:
        return jsonify({"ok": False,
                        "error": "months is not one of your presets — "
                                 "pick a preset tier or enter a custom "
                                 "GB"}), 400
    total_bytes = (GB_BYTES[int(gb)] if float(gb).is_integer()
                   and int(gb) in GB_BYTES else int(gb * 1073741824))
    # explicit full name override (testing / advanced); otherwise build from
    # the customer name + the expiry date in his convention.
    full_name = (data.get("client_name") or "").strip()
    if full_name:
        name = full_name
        if custom_date:
            exp_date = custom_date
        else:
            exp_date, _suffix = expiry_for(months)
    else:
        raw = (data.get("name") or "").strip()
        raw = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if not raw:
            return jsonify({"ok": False, "error": "customer name needed"}), 400
        if custom_date:
            exp_date = custom_date
            suffix = "%d_%d_%d" % (exp_date.day, exp_date.month,
                                   exp_date.year)
        else:
            exp_date, suffix = expiry_for(months)
        name = "%s__%s" % (raw, suffix)
    try:
        client = make_client(server)
        if server["type"] == "xui":
            exp_ms = int((datetime(exp_date.year, exp_date.month, exp_date.day,
                                   tzinfo=MM) + timedelta(days=1)).timestamp() * 1000)
            created = client.create_client(name, total_bytes, exp_ms)
            exp_ts = exp_ms // 1000
        else:
            exp_s = int(datetime(exp_date.year, exp_date.month, exp_date.day,
                                 23, 59, tzinfo=MM).timestamp())
            created = client.create_user(name, total_bytes, exp_s)
            exp_ts = exp_s
        link = client.build_link(created)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    note_key_created(server_id, name)
    HEALTH_CACHE.pop(server_id, None)
    gb_disp = str(int(gb)) if float(gb).is_integer() else str(gb)
    if is_preset and custom_date is None:
        summary = "%dM • %s GB • exp %s" % (months, gb_disp,
                                            exp_date.isoformat())
    else:
        summary = "%s GB • exp %s" % (gb_disp, exp_date.isoformat())
    return jsonify({"ok": True, "name": name, "link": link,
                    "expiry": exp_date.isoformat(), "expiry_ts": exp_ts,
                    "expiry_h": exp_date.strftime("%d %b %Y"),
                    "gb": gb, "months": months,
                    "limit": total_bytes, "created_at": int(time.time()),
                    "custom": bool(not is_preset
                                  or custom_date is not None),
                    "summary": summary})


@app.route("/api/server/<int:server_id>/delete-key", methods=["POST"])
@login_required
def api_delete_key(server_id):
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "client name needed"}), 400
    try:
        client = make_client(server)
        if server["type"] == "xui":
            # v6.1.5: deletes BOTH sides of a dual customer.
            client.delete_merged_client(name)
        else:
            client.delete_user(name)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    note_key_deleted(server_id, name)
    HEALTH_CACHE.pop(server_id, None)
    invalidate_overview()  # v6.1.2: keep dashboard sections from resurrecting this row
    return jsonify({"ok": True, "name": name, "gone": True})


# ---------------------------------------------------------------- key actions (v2)
def _key_action_server(server_id):
    return _owned_server(server_id)


def _merged_links(client, rec):
    """v6.1.5: (link, links) for a merged 3X-UI record — the Outline
    ss:// link first; the VLESS links dict when a VLESS side exists
    (else None). A VLESS-only record's link is its WS primary link."""
    links = None
    if rec.get("vless"):
        try:
            links = client.build_vless_links(rec["vless"])
        except Exception:  # noqa: BLE001
            links = None
    if rec.get("outline"):
        link = client.build_link(rec["outline"])
    elif links:
        link = links.get("primary") or ""
    else:
        link = ""
    return link, links


@app.route("/api/server/<int:server_id>/clients/<path:name>/renew",
           methods=["POST"])
@login_required
def api_renew_key(server_id, name):
    """Renew +1..+6 months: new expiry = old expiry + 30..180 days.
    The key is RENAMED to the new date (the name encodes the expiry) and
    the expiry moves in the same update call. GB is unchanged."""
    server, err = _key_action_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    try:
        months = int(data.get("months") or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 6:
        return jsonify({"ok": False, "error": "months must be 1..6"}), 400
    try:
        client = make_client(server)
        if server["type"] == "xui":
            # v6.1.5: renews BOTH sides of a dual customer (same new
            # name + expiry); a single-protocol customer renews its
            # one side.
            rec, new_name = client.renew_merged_client(name, months)
            link, links = _merged_links(client, rec)
        else:
            rec, new_name = client.renew_user(name, months)
            link = client.build_link(rec)
            links = None
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    if new_name != name:
        note_key_renamed(server_id, name, new_name)
    HEALTH_CACHE.pop(server_id, None)
    resp = {"ok": True, "old_name": name, "name": new_name,
            "link": link,
            "expiry": rec.get("expiry") or 0,
            "expiry_h": (datetime.fromtimestamp(rec["expiry"], MM)
                         .strftime("%d %b %Y") if rec.get("expiry")
                         else "never"),
            "gb": int((rec.get("limit") or 0) // 1073741824),
            "months": months}
    if links:
        resp["links"] = links
    if server["type"] == "xui":
        resp["protocol"] = rec.get("protocol", "outline")
    return jsonify(resp)


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
            # v6.1.5: swaps BOTH sides of a dual customer (fresh
            # Outline password + fresh VLESS UUID).
            rec = client.swap_merged_client(name)
            link, links = _merged_links(client, rec)
        else:
            rec = client.swap_user(name)
            link = client.build_link(rec)
            links = None
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    # Swap = delete + fresh create, so it is a NEW key: fresh created_at.
    note_key_created(server_id, name)
    HEALTH_CACHE.pop(server_id, None)
    resp = {"ok": True, "name": name, "link": link,
            "expiry": rec.get("expiry") or 0,
            "limit": rec.get("limit") or 0,
            "created_at": int(time.time())}
    if links:
        resp["links"] = links
    if server["type"] == "xui":
        resp["protocol"] = rec.get("protocol", "outline")
    return jsonify(resp)


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
            # v6.1.5: toggles BOTH sides of a dual customer.
            rec = client.toggle_merged_client(name, enable)
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
            # v6.1.5: sets the GB limit on BOTH sides of a dual customer.
            rec = client.set_gb_merged_client(name, GB_BYTES[gb])
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
            # v6.1.5: edits BOTH sides of a dual customer (a date
            # change renames both records to the same new date).
            rec, new_name = client.edit_merged_client(name, gb_bytes,
                                                      new_date)
            link, links = _merged_links(client, rec)
        else:
            rec, new_name = client.edit_user(name, gb_bytes, new_date)
            link = client.build_link(rec)
            links = None
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    if new_name != name:
        note_key_renamed(server_id, name, new_name)
    HEALTH_CACHE.pop(server_id, None)
    resp = {"ok": True, "old_name": name, "name": new_name,
            "link": link,
            "expiry": rec.get("expiry") or 0,
            "expiry_h": (datetime.fromtimestamp(rec["expiry"], MM)
                         .strftime("%d %b %Y") if rec.get("expiry")
                         else "never"),
            "gb": int((rec.get("limit") or 0) // 1073741824)}
    if links:
        resp["links"] = links
    if server["type"] == "xui":
        resp["protocol"] = rec.get("protocol", "outline")
    return jsonify(resp)


def _parse_ss_link(q):
    """Parse a pasted ss:// key link -> (name, password); either may be None.
    Handles both fragment styles: this panel's own `#<name>` and the
    3X-UI panel's `#Outline-<name>-<GB>GB...` style. The password (from
    the base64 userinfo) identifies the key exactly; the name is the
    fallback. Returns (None, None) when q is not an ss:// link."""
    s = (q or "").strip()
    if not s.lower().startswith("ss://"):
        return None, None
    rest = s[5:]
    password = None
    if "@" in rest:
        userinfo = rest.split("@", 1)[0].split("?", 1)[0].split("#", 1)[0]
        try:
            padded = userinfo + "=" * (-len(userinfo) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", "replace")
            if ":" in decoded:
                password = decoded.split(":", 1)[1] or None
        except Exception:  # noqa: BLE001 - malformed link: fall back to name
            password = None
    name = None
    if "#" in s:
        frag = urllib.parse.unquote(s.split("#", 1)[1].strip())
        m = re.match(r"^Outline-(.+)-\d+GB", frag)
        name = m.group(1) if m else (frag or None)
    return name, password


def _parse_vless_link(q):
    """Parse a pasted vless:// key link -> (name, uuid); either may be
    None. The UUID (userinfo) identifies the key exactly; the name is
    the fallback. Returns (None, None) when q is not a vless:// link."""
    s = (q or "").strip()
    if not s.lower().startswith("vless://"):
        return None, None
    rest = s[8:]
    uid = rest.split("@", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    name = None
    if "#" in s:
        frag = urllib.parse.unquote(s.split("#", 1)[1].strip())
        name = frag or None
    return name, uid or None


@app.route("/api/search")
@login_required
def api_search():
    q_raw = (request.args.get("q") or "").strip()
    if len(q_raw) < 2:
        return jsonify({"ok": True, "results": []})
    # Pasted a whole key link? Match by its name or its password/UUID.
    link_name, link_pw = _parse_ss_link(q_raw)
    vless_name, vless_uuid = _parse_vless_link(q_raw)
    q = q_raw.lower()
    results = []
    for s in list_servers():
        full = get_server(s["id"])
        try:
            clients = server_all_clients(full)
        except Exception:  # noqa: BLE001 - one down server must not break search
            continue
        created = created_map(s["id"])
        for c in clients:
            if (link_name is not None or link_pw is not None
                    or vless_name is not None or vless_uuid is not None):
                hit = ((link_name and c["name"].lower() == link_name.lower())
                       or (link_pw and c.get("password")
                           and c["password"] == link_pw)
                       or (vless_uuid and c.get("uuid")
                           and c["uuid"] == vless_uuid)
                       or (vless_name and c["name"].lower()
                           == vless_name.lower()))
            else:
                hit = q in c["name"].lower()
            if hit:
                pc = public_client(s["id"], c, created.get(c["name"]))
                pc["server_name"] = s["name"]
                pc["server_type"] = s["type"]
                results.append(pc)
    results.sort(key=lambda r: r["name"].lower())
    return jsonify({"ok": True, "results": results[:100]})


@app.route("/api/expiring")
@login_required
def api_expiring():
    days = int(request.args.get("days") or 7)
    now = time.time()
    # v6: the cache is per owner — a reseller must never see another
    # owner's cached expiring keys.
    cache = EXPIRING_CACHE.get(owner_key())
    if cache and now - cache[0] < 60:
        return jsonify({"ok": True, "results": cache[1][:100]})
    soon = now + days * 86400
    # v4.2: scan the visible servers in PARALLEL (was one by one: 12 x
    # 2-4s = 30-60s per call, blocking a gunicorn worker the whole time).
    # DB reads (server rows + created maps) happen here first — get_db
    # needs the request context, so only the network scans run in the pool.
    servers = [get_server(s["id"]) for s in list_servers()]
    created_maps = {s["id"]: created_map(s["id"]) for s in servers}

    def scan(full):
        try:
            clients = server_all_clients(full)
        except Exception:  # noqa: BLE001
            return []
        created = created_maps[full["id"]]
        out = []
        for c in clients:
            exp = c.get("expiry") or 0
            if exp and now <= exp <= soon:
                pc = public_client(full["id"], c, created.get(c["name"]))
                pc["server_name"] = full["name"]
                pc["days_left"] = max(0, int((exp - now) // 86400))
                out.append(pc)
        return out

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for part in pool.map(scan, servers):
            results.extend(part)
    results.sort(key=lambda r: r["expiry"])
    EXPIRING_CACHE[owner_key()] = [now, results]
    return jsonify({"ok": True, "results": results[:100]})


# ---------------------------------------------------------------- v6.1 key overview
LOW_GB_BYTES = 10 * 1073741824  # 10 GB — keys at/below this remaining are "low GB"
OVERVIEW_LIST_CAP = 100


@app.route("/api/key-overview")
@login_required
def api_key_overview():
    """One parallel scan of all visible servers computing the dashboard
    stats + the three key lists in a single pass (replaces the old
    7-day /api/expiring scan for the dashboard):
      stats: active keys, keys expiring within 3 days, expired keys
        (TRUE totals across all visible servers — feeds the 4 stat cards),
        plus "online" (v6.1.3): keys connected right now, counted from
        the same scan (3X-UI onlines endpoint / Marzban online_at) —
        feeds the "Online now" strip. A reseller's stats cover only
        their own servers, like everything else in this payload.
      by_owner: owner -> {totals, expiring, low_gb, expired} — the same
        three lists SCOPED to one owner (the Super Admin's own servers,
        or one reseller's servers). The dashboard renders the three
        sections inside each owner/channel group from its own bucket, so
        a section header count is that owner's TRUE total and agrees
        with the stat cards; each list is capped at 100 rows.
      per_server_online (v6.1.4): server_id -> keys connected right
        now on THAT server, counted from the same scan with the same
        per-client check as stats["online"] (so the per-server counts
        sum to stats["online"]); null when that server's scan failed
        (down/unreachable) — the client never shows a fake number.
        A reseller's map covers only their own servers.
      total (v6.1.2): the same three lists MERGED across all visible
        servers (the admin's 54; a reseller's own servers only), same
        sort orders, same 100-row cap; totals are the TRUE global
        counts (they agree with stats). The dashboard renders these as
        the TOTAL EXPIRING / TOTAL LOW GB / TOTAL EXPIRED sections
        below the MY SERVERS / OTHER groups (admin only). Built
        server-side so the client never sums per-owner lists (it would
        double-count the capped lists).
      expiring: not expired, expiry within 3 days (soonest first).
      low_gb: not expired, 0 < remaining <= 10 GB (least remaining first).
      expired: expiry passed OR used >= limit (most recently expired
        first); each row carries expired_reason "date" or "gb".
    Classification (per the user's rules):
      expired = (expiry and expiry <= now) OR (limit and used >= limit)
      active  = enable AND expiry in the future AND used < limit
    Lists are capped at 100 rows each; the stats/totals counts are the
    TRUE totals, not the capped lengths."""
    now = time.time()
    cache = OVERVIEW_CACHE.get(owner_key())
    if cache and now - cache[0] < 60:
        return jsonify(cache[1])
    servers = [get_server(s["id"]) for s in list_servers()]
    created_maps = {s["id"]: created_map(s["id"]) for s in servers}

    def scan(full):
        # v6.1.3: one client per server — the client list plus, for
        # 3X-UI, the currently-connected emails (one extra POST in the
        # same worker thread; Marzban users already carry "online"
        # from online_at). Feeds stats["online"] with no extra browser
        # requests; the whole payload is cached 60s like before.
        try:
            client = make_client(full)
            if full["type"] == "xui":
                # v6.1.5: ONE record per customer (email) — a dual
                # Outline+VLESS customer counts once in every total
                # below (total/active/expiring/expired/low/online).
                clients = client.list_merged_clients()
                try:
                    online = client.online_emails()
                except Exception:  # noqa: BLE001
                    online = None
                return clients, online, True
            return client.list_users(), None, True
        except Exception:  # noqa: BLE001
            return [], None, False

    def bucket(buckets, owner):
        return buckets.setdefault(
            owner, {"n_expiring": 0, "n_low": 0, "n_expired": 0,
                    "expiring": [], "low_gb": [], "expired": []})

    active = 0
    total = 0
    n_online = 0
    n_expiring = n_low = n_expired = 0
    per_server_online = {}
    buckets = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for full, (clients, online, ok) in zip(
                servers, pool.map(scan, servers)):
            created = created_maps[full["id"]]
            b = bucket(buckets, full["owner"])
            n_server_online = 0
            for c in clients:
                total += 1
                if c.get("online") is True or (
                        online is not None and c.get("name") in online):
                    n_online += 1
                    n_server_online += 1
                exp = c.get("expiry") or 0
                used = c.get("used") or 0
                limit = c.get("limit") or 0
                enable = c.get("enable", True)
                is_expired = bool((exp and exp <= now) or
                                  (limit and used >= limit))
                if is_expired:
                    n_expired += 1
                    b["n_expired"] += 1
                    pc = public_client(full["id"], c,
                                       created.get(c["name"]))
                    pc["server_name"] = full["name"]
                    pc["expired_reason"] = ("date" if exp and exp <= now
                                            else "gb")
                    b["expired"].append(pc)
                    continue
                if enable and (not exp or exp > now) and \
                        (not limit or used < limit):
                    active += 1
                if exp and now < exp <= now + 3 * 86400:
                    n_expiring += 1
                    b["n_expiring"] += 1
                    pc = public_client(full["id"], c,
                                       created.get(c["name"]))
                    pc["server_name"] = full["name"]
                    pc["days_left"] = max(0, int((exp - now) // 86400))
                    b["expiring"].append(pc)
                if limit and 0 < (limit - used) <= LOW_GB_BYTES:
                    n_low += 1
                    b["n_low"] += 1
                    pc = public_client(full["id"], c,
                                       created.get(c["name"]))
                    pc["server_name"] = full["name"]
                    pc["remaining"] = limit - used
                    pc["remaining_h"] = fmt_gb(limit - used)
                    b["low_gb"].append(pc)
            # v6.1.4: per-server online count from the same scan; null
            # when the scan failed (server down) — never a fake number.
            per_server_online[full["id"]] = (
                n_server_online if ok else None)
    by_owner = {}
    for owner, b in buckets.items():
        b["expiring"].sort(key=lambda r: r["expiry"])
        b["low_gb"].sort(key=lambda r: r["remaining"])
        b["expired"].sort(key=lambda r: r["expiry"], reverse=True)
        by_owner[owner] = {
            "totals": {"expiring_3d": b["n_expiring"],
                       "low_gb": b["n_low"], "expired": b["n_expired"]},
            "expiring": b["expiring"][:OVERVIEW_LIST_CAP],
            "low_gb": b["low_gb"][:OVERVIEW_LIST_CAP],
            "expired": b["expired"][:OVERVIEW_LIST_CAP]}
    # v6.1.2: the "total" aggregate — the same three lists merged
    # across ALL visible servers, same sort orders, same 100-row cap.
    # The bucket lists are still the FULL (uncapped) lists here; the
    # caps above only sliced copies into by_owner.
    total_bucket = {
        "totals": {"expiring_3d": n_expiring, "low_gb": n_low,
                   "expired": n_expired},
        "expiring": sorted(
            (pc for b in buckets.values() for pc in b["expiring"]),
            key=lambda r: r["expiry"])[:OVERVIEW_LIST_CAP],
        "low_gb": sorted(
            (pc for b in buckets.values() for pc in b["low_gb"]),
            key=lambda r: r["remaining"])[:OVERVIEW_LIST_CAP],
        "expired": sorted(
            (pc for b in buckets.values() for pc in b["expired"]),
            key=lambda r: r["expiry"],
            reverse=True)[:OVERVIEW_LIST_CAP]}
    payload = {"ok": True,
               "stats": {"total": total, "active": active,
                         "expiring_3d": n_expiring, "low_gb": n_low,
                         "expired": n_expired, "online": n_online},
               "per_server_online": {str(k): v
                                     for k, v in per_server_online.items()},
               "by_owner": by_owner,
               "total": total_bucket}
    OVERVIEW_CACHE[owner_key()] = [now, payload]
    return jsonify(payload)


@app.route("/api/server/<int:server_id>/test", methods=["POST"])
@login_required
def api_test(server_id):
    server, err = _owned_server(server_id)
    if err:
        return err
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
@require_admin
def api_add_server():
    data = request.get_json(force=True, silent=True) or {}
    for field in ("name", "type", "base_url", "share_host"):
        if not (data.get(field) or "").strip():
            return jsonify({"ok": False, "error": "%s is required" % field}), 400
    if data["type"] not in ("xui", "marzban"):
        return jsonify({"ok": False, "error": "type must be xui or marzban"}), 400
    owner = (data.get("owner") or "admin").strip()
    if owner != "admin" and not get_user(owner):
        return jsonify({"ok": False,
                        "error": "owner is not a known user"}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO servers (name, country, type, base_url, panel_path,
           admin_user, admin_pw_enc, share_host, share_port, is_free,
           is_sales, sort_order, created_at, owner)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data["name"].strip(), (data.get("country") or "").strip(),
         data["type"], data["base_url"].strip().rstrip("/"),
         (data.get("panel_path") or "").strip(),
         (data.get("admin_user") or "kohein").strip(),
         encrypt_pw(data.get("admin_pw") or ""),
         data["share_host"].strip(), int(data.get("share_port") or 1080),
         1 if data.get("is_free") else 0, 1 if data.get("is_sales") else 0,
         int(data.get("sort_order") or 0), int(time.time()), owner))
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/servers/<int:server_id>", methods=["PUT"])
@login_required
@require_admin
def api_edit_server(server_id):
    if not get_server(server_id):
        return jsonify({"ok": False, "error": "server not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    if "owner" in data:
        owner = (data.get("owner") or "admin").strip()
        if owner != "admin" and not get_user(owner):
            return jsonify({"ok": False,
                            "error": "owner is not a known user"}), 400
        data["owner"] = owner
    db = get_db()
    fields, values = [], []
    for col in ("name", "country", "type", "base_url", "panel_path",
                "admin_user", "share_host", "share_port", "is_free",
                "is_sales", "sort_order", "owner"):
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
@require_admin
def api_delete_server(server_id):
    db = get_db()
    db.execute("DELETE FROM servers WHERE id=?", (server_id,))
    db.commit()
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- VLESS (v6, 3X-UI only)
@app.route("/api/server/<int:server_id>/vless-clients")
@login_required
def api_vless_clients(server_id):
    server, err = _owned_server(server_id)
    if err:
        return err
    if server["type"] != "xui":
        return jsonify({"ok": True, "server": server["name"], "clients": []})
    try:
        client = make_client(server)
        clients = client.list_vless_clients()
        # v6.1: VLESS clients are matched by email, same as Outline.
        online = client.online_emails()
        if online is not None:
            for c in clients:
                c["online"] = c["name"] in online
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    created = created_map(server_id)
    return jsonify({"ok": True, "server": server["name"],
                    "clients": [public_client(server_id, c,
                                              created.get(c["name"]))
                                for c in clients]})


@app.route("/api/server/<int:server_id>/vless-link/<path:name>")
@login_required
def api_vless_link(server_id, name):
    server, err = _owned_server(server_id)
    if err:
        return err
    if server["type"] != "xui":
        return jsonify({"ok": False, "error": "VLESS is 3X-UI only"}), 400
    try:
        client = make_client(server)
        c = client.get_vless_client(name)
        if not c:
            return jsonify({"ok": False, "error": "client not found"}), 404
        links = client.build_vless_links(c)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    return jsonify({"ok": True, "name": name, "links": links,
                    "link": links["primary"]})


@app.route("/api/server/<int:server_id>/add-vless-key", methods=["POST"])
@login_required
def api_add_vless_key(server_id):
    """Create a VLESS key: the SAME UUID on every vless inbound.
    Same name/GB/date rules as the Outline add-key route."""
    server, err = _owned_server(server_id)
    if err:
        return err
    if server["type"] != "xui":
        return jsonify({"ok": False, "error": "VLESS is 3X-UI only"}), 400
    data = request.get_json(force=True, silent=True) or {}
    custom_gb = None
    if data.get("gb") is not None:
        try:
            custom_gb = float(data.get("gb"))
        except (TypeError, ValueError):
            custom_gb = 0
        if not custom_gb or custom_gb <= 0 or custom_gb > 100000:
            return jsonify({"ok": False,
                            "error": "gb must be a positive number"}), 400
    custom_date = None
    if data.get("expiry_date"):
        try:
            custom_date = datetime.strptime(
                str(data["expiry_date"]).strip(), "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False,
                            "error": "expiry_date must be YYYY-MM-DD"}), 400
    try:
        months = int(data.get("months") or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 12:
        return jsonify({"ok": False, "error": "months must be 1..12"}), 400
    # v6.1.6: GB from the caller's own VLESS preset tier (see
    # resolve_preset_gb); a GB matching no tier is a custom GB.
    gb, is_preset = resolve_preset_gb(current_user()["username"],
                                      "vless", months, custom_gb)
    if gb is None:
        return jsonify({"ok": False,
                        "error": "months is not one of your presets — "
                                 "pick a preset tier or enter a custom "
                                 "GB"}), 400
    total_bytes = (GB_BYTES[int(gb)] if float(gb).is_integer()
                   and int(gb) in GB_BYTES else int(gb * 1073741824))
    full_name = (data.get("client_name") or "").strip()
    if full_name:
        name = full_name
        if custom_date:
            exp_date = custom_date
        else:
            exp_date, _suffix = expiry_for(months)
    else:
        raw = (data.get("name") or "").strip()
        raw = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if not raw:
            return jsonify({"ok": False, "error": "customer name needed"}), 400
        if custom_date:
            exp_date = custom_date
            suffix = "%d_%d_%d" % (exp_date.day, exp_date.month,
                                   exp_date.year)
        else:
            exp_date, suffix = expiry_for(months)
        name = "%s__%s" % (raw, suffix)
    try:
        client = make_client(server)
        exp_ms = int((datetime(exp_date.year, exp_date.month, exp_date.day,
                               tzinfo=MM) + timedelta(days=1)).timestamp() * 1000)
        created = client.create_vless_client(name, total_bytes, exp_ms)
        links = client.build_vless_links(created)
        exp_ts = exp_ms // 1000
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    note_key_created(server_id, name)
    HEALTH_CACHE.pop(server_id, None)
    gb_disp = str(int(gb)) if float(gb).is_integer() else str(gb)
    if is_preset and custom_date is None:
        summary = "VLESS • %dM • %s GB • exp %s" % (
            months, gb_disp, exp_date.isoformat())
    else:
        summary = "VLESS • %s GB • exp %s" % (gb_disp,
                                              exp_date.isoformat())
    return jsonify({"ok": True, "name": name, "links": links,
                    "link": links["primary"],
                    "expiry": exp_date.isoformat(), "expiry_ts": exp_ts,
                    "expiry_h": exp_date.strftime("%d %b %Y"),
                    "gb": gb, "months": months,
                    "limit": total_bytes, "created_at": int(time.time()),
                    "custom": bool(not is_preset
                                  or custom_date is not None),
                    "summary": summary})


# ---------------------------------------------------------------- v6.1 bulk keys
@app.route("/api/server/<int:server_id>/bulk-add-key", methods=["POST"])
@login_required
def api_bulk_add_key(server_id):
    """Create N keys at once: {base}-{i}___DD_MM_YYYY, i = 1..N.
    The GB comes from the caller's own preset tier for the chosen
    protocol and months (v6.1.6); custom gb / expiry_date override
    the preset. protocol "vless" (3X-UI only) gives each key the
    same-UUID-on-all-VLESS-inbounds treatment as a single VLESS
    create. Creates stop at the first failure and the response lists
    what was already made."""
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    protocol = (data.get("protocol") or "outline").strip().lower()
    if protocol not in ("outline", "vless"):
        return jsonify({"ok": False,
                        "error": "protocol must be outline or vless"}), 400
    if protocol == "vless" and server["type"] != "xui":
        return jsonify({"ok": False,
                        "error": "VLESS is 3X-UI only"}), 400
    base = (data.get("base_name") or data.get("name") or "").strip()
    base = "".join(ch for ch in base if ch.isalnum() or ch in "_-")
    if not base:
        return jsonify({"ok": False, "error": "base name needed"}), 400
    try:
        count = int(data.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 1 or count > 50:
        return jsonify({"ok": False,
                        "error": "count must be 1..50"}), 400
    custom_gb = None
    if data.get("gb") is not None:
        try:
            custom_gb = float(data.get("gb"))
        except (TypeError, ValueError):
            custom_gb = 0
        if not custom_gb or custom_gb <= 0 or custom_gb > 100000:
            return jsonify({"ok": False,
                            "error": "gb must be a positive number"}), 400
    custom_date = None
    if data.get("expiry_date"):
        try:
            custom_date = datetime.strptime(
                str(data["expiry_date"]).strip(), "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False,
                            "error": "expiry_date must be YYYY-MM-DD"}), 400
    try:
        months = int(data.get("months") or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 12:
        return jsonify({"ok": False, "error": "months must be 1..12"}), 400
    # v6.1.6: GB from the caller's own preset tier for this protocol.
    gb, is_preset = resolve_preset_gb(current_user()["username"],
                                      protocol, months, custom_gb)
    if gb is None:
        return jsonify({"ok": False,
                        "error": "months is not one of your presets — "
                                 "pick a preset tier or enter a custom "
                                 "GB"}), 400
    total_bytes = (GB_BYTES[int(gb)] if float(gb).is_integer()
                   and int(gb) in GB_BYTES else int(gb * 1073741824))
    if custom_date:
        exp_date = custom_date
        suffix = "%d_%d_%d" % (exp_date.day, exp_date.month, exp_date.year)
    else:
        exp_date, suffix = expiry_for(months)
    try:
        client = make_client(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    created_keys = []
    for i in range(1, count + 1):
        name = "%s-%d__%s" % (base, i, suffix)
        try:
            if server["type"] == "xui":
                exp_ms = int((datetime(exp_date.year, exp_date.month,
                                       exp_date.day, tzinfo=MM)
                              + timedelta(days=1)).timestamp() * 1000)
                if protocol == "vless":
                    rec = client.create_vless_client(name, total_bytes,
                                                     exp_ms)
                    links = client.build_vless_links(rec)
                    link = links["primary"]
                else:
                    rec = client.create_client(name, total_bytes, exp_ms)
                    link = client.build_link(rec)
            else:
                exp_s = int(datetime(exp_date.year, exp_date.month,
                                     exp_date.day, 23, 59,
                                     tzinfo=MM).timestamp())
                rec = client.create_user(name, total_bytes, exp_s)
                link = client.build_link(rec)
        except Exception as exc:  # noqa: BLE001 - stop at first failure
            HEALTH_CACHE.pop(server_id, None)
            return jsonify({"ok": False, "error": str(exc)[:300],
                            "created": created_keys,
                            "created_count": len(created_keys)}), 502
        note_key_created(server_id, name)
        created_keys.append({"name": name, "link": link,
                             "protocol": protocol})
    HEALTH_CACHE.pop(server_id, None)
    return jsonify({"ok": True, "created": created_keys,
                    "created_count": len(created_keys),
                    "expiry": exp_date.isoformat(),
                    "expiry_h": exp_date.strftime("%d %b %Y"),
                    "gb": gb, "months": months,
                    "limit": total_bytes})


@app.route("/api/server/<int:server_id>/bulk-delete-key", methods=["POST"])
@login_required
def api_bulk_delete_key(server_id):
    """Delete several keys at once. Body: {"items": [{"name": ...,
    "protocol": "outline"|"vless"|"both"}]} (protocol defaults to
    outline; Marzban servers ignore it). v6.1.5: protocol "both"
    deletes BOTH sides of a dual customer. The UI always asks for
    confirmation with the count before calling this. Per-key failures
    are reported in "failed" — the rest still delete."""
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or data.get("names") or []
    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({"ok": False,
                        "error": "items must be a list of 1..100 keys"}), 400
    try:
        client = make_client(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    deleted, failed = [], []
    for it in items:
        if isinstance(it, dict):
            name = (it.get("name") or "").strip()
            proto = (it.get("protocol") or "outline").strip().lower()
        else:
            name, proto = str(it).strip(), "outline"
        if not name:
            failed.append({"name": name, "error": "empty name"})
            continue
        try:
            if server["type"] == "marzban":
                client.delete_user(name)
            elif proto == "vless":
                client.delete_vless_client(name)
            elif proto == "both":
                client.delete_merged_client(name)
            else:
                client.delete_client(name)
            note_key_deleted(server_id, name)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": name, "error": str(exc)[:120]})
    HEALTH_CACHE.pop(server_id, None)
    invalidate_overview()  # v6.1.2: keep dashboard sections from resurrecting deleted rows
    return jsonify({"ok": True, "deleted": deleted, "failed": failed})


@app.route("/api/server/<int:server_id>/bulk-renew-key", methods=["POST"])
@login_required
def api_bulk_renew_key(server_id):
    """v6.1.3: renew several EXPIRING keys at once. Body:
    {"items": [{"name": ..., "protocol": "outline"|"vless"|"both"}],
    "months": 1..6}. v6.1.5: protocol "both" renews BOTH sides of a
    dual customer. Reuses the single-renew semantics per key (new
    expiry = old + 30..180 days, key RENAMED to the new date, GB
    unchanged; Marzban renew = DELETE+RECREATE). The UI asks for
    confirmation with the count + months before calling this.
    Per-key failures are reported in "failed" — the rest still renew.
    Returns {"renewed": [{old_name, name, expiry, expiry_h, gb,
    months}], "failed": [{name, error}]}."""
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    try:
        months = int(data.get("months") or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 6:
        return jsonify({"ok": False, "error": "months must be 1..6"}), 400
    items = data.get("items") or data.get("names") or []
    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({"ok": False,
                        "error": "items must be a list of 1..100 keys"}), 400
    try:
        client = make_client(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    renewed, failed = [], []
    for it in items:
        if isinstance(it, dict):
            name = (it.get("name") or "").strip()
            proto = (it.get("protocol") or "outline").strip().lower()
        else:
            name, proto = str(it).strip(), "outline"
        if not name:
            failed.append({"name": name, "error": "empty name"})
            continue
        try:
            if server["type"] == "marzban":
                rec, new_name = client.renew_user(name, months)
            elif proto == "vless":
                rec, new_name = client.renew_vless_client(name, months)
            elif proto == "both":
                rec, new_name = client.renew_merged_client(name, months)
            else:
                rec, new_name = client.renew_client(name, months)
            if new_name != name:
                note_key_renamed(server_id, name, new_name)
            renewed.append({
                "old_name": name, "name": new_name,
                "expiry": rec.get("expiry") or 0,
                "expiry_h": (datetime.fromtimestamp(rec["expiry"], MM)
                             .strftime("%d %b %Y") if rec.get("expiry")
                             else "never"),
                "gb": int((rec.get("limit") or 0) // 1073741824),
                "months": months})
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": name, "error": str(exc)[:120]})
    HEALTH_CACHE.pop(server_id, None)
    invalidate_overview()  # renewed keys leave the expiring lists
    return jsonify({"ok": True, "renewed": renewed, "failed": failed})


@app.route("/api/server/<int:server_id>/key-ips", methods=["POST"])
@login_required
def api_key_ips(server_id):
    """v6.1.3: distinct-IP counts for up to 100 keys, for the
    key-sharing flag. Body: {"names": [...]}. 3X-UI only — Marzban
    has no IP log API, so it answers supported=false and the UI says
    so. The panel is fanned out to with 5 workers; a per-key failure
    reports count null (unknown), never 0."""
    server, err = _owned_server(server_id)
    if err:
        return err
    if server["type"] != "xui":
        return jsonify({"ok": True, "supported": False, "counts": {}})
    data = request.get_json(force=True, silent=True) or {}
    names = data.get("names") or []
    if not isinstance(names, list) or not names or len(names) > 100:
        return jsonify({"ok": False,
                        "error": "names must be a list of 1..100 keys"}), 400
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return jsonify({"ok": False,
                        "error": "names must be a list of 1..100 keys"}), 400
    try:
        client = make_client(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502

    def one(name):
        try:
            res = client.client_ips(name)
            return name, (res["count"] if res else None)
        except Exception:  # noqa: BLE001
            return name, None

    counts = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for name, count in pool.map(one, names):
            counts[name] = count
    return jsonify({"ok": True, "supported": True, "counts": counts})


@app.route("/api/server/<int:server_id>/export")
@login_required
def api_export_server(server_id):
    """v6.1.3: download ALL keys on one server as CSV (backup).
    Columns: name, link, vless_link, expiry, gb_limit, used_gb,
    status. v6.1.5: one row per customer (dual customers used to
    appear twice); their VLESS link goes in the vless_link column so
    the backup loses no key. UTF-8 with BOM so Excel opens it
    correctly. Ownership is enforced by _owned_server (admin: any
    server; reseller: own servers only). A row whose link cannot be
    built (no password on record) is still listed, with an empty
    link."""
    server, err = _owned_server(server_id)
    if err:
        return err
    try:
        client = make_client(server)
        clients = server_all_clients(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    rows = []
    for c in clients:
        link, vlink = "", ""
        try:
            if c.get("protocol") == "vless":
                links = client.build_vless_links(c)
                link = links.get("primary") or links.get("ws") or ""
            else:
                link = client.build_link(c)
                if c.get("vless"):
                    vlinks = client.build_vless_links(c["vless"])
                    vlink = vlinks.get("primary") or vlinks.get("ws") or ""
        except Exception:  # noqa: BLE001
            pass
        exp = c.get("expiry") or 0
        rows.append([
            c.get("name", ""),
            link,
            vlink,
            (datetime.fromtimestamp(exp, MM).strftime("%Y-%m-%d")
             if exp else "never"),
            str(int((c.get("limit") or 0) // 1073741824)),
            "%.2f" % ((c.get("used") or 0) / 1073741824),
            c.get("status") or "",
        ])
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM — Excel needs it to read UTF-8
    w = csv.writer(buf)
    w.writerow(["name", "link", "vless_link", "expiry", "gb_limit",
                "used_gb", "status"])
    w.writerows(rows)
    safe = "".join(ch for ch in server["name"]
                   if ch.isalnum() or ch in "_-") or "server"
    resp = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="%s-keys.csv"' % safe)
    return resp


@app.route("/api/server/<int:server_id>/delete-vless-key", methods=["POST"])
@login_required
def api_delete_vless_key(server_id):
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "client name needed"}), 400
    try:
        client = make_client(server)
        client.delete_vless_client(name)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    note_key_deleted(server_id, name)
    HEALTH_CACHE.pop(server_id, None)
    invalidate_overview()  # v6.1.2: keep dashboard sections from resurrecting this row
    return jsonify({"ok": True, "name": name, "gone": True})


def _vless_action_response(name, rec, links):
    return {"ok": True, "name": name, "links": links,
            "link": links["primary"],
            "expiry": rec.get("expiry") or 0,
            "expiry_h": (datetime.fromtimestamp(rec["expiry"], MM)
                         .strftime("%d %b %Y") if rec.get("expiry")
                         else "never"),
            "gb": int((rec.get("limit") or 0) // 1073741824),
            "limit": rec.get("limit") or 0,
            "enable": rec.get("enable"),
            "status": rec.get("status")}


@app.route("/api/server/<int:server_id>/vless-clients/<path:name>/renew",
           methods=["POST"])
@login_required
def api_renew_vless_key(server_id, name):
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    try:
        months = int(data.get("months") or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 6:
        return jsonify({"ok": False, "error": "months must be 1..6"}), 400
    try:
        client = make_client(server)
        rec, new_name = client.renew_vless_client(name, months)
        links = client.build_vless_links(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    if new_name != name:
        note_key_renamed(server_id, name, new_name)
    HEALTH_CACHE.pop(server_id, None)
    resp = _vless_action_response(new_name, rec, links)
    resp["old_name"] = name
    resp["months"] = months
    return jsonify(resp)


@app.route("/api/server/<int:server_id>/vless-clients/<path:name>/swap",
           methods=["POST"])
@login_required
def api_swap_vless_key(server_id, name):
    server, err = _owned_server(server_id)
    if err:
        return err
    try:
        client = make_client(server)
        rec = client.swap_vless_client(name)
        links = client.build_vless_links(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    note_key_created(server_id, name)
    HEALTH_CACHE.pop(server_id, None)
    resp = _vless_action_response(name, rec, links)
    resp["created_at"] = int(time.time())
    return jsonify(resp)


@app.route("/api/server/<int:server_id>/vless-clients/<path:name>/toggle",
           methods=["POST"])
@login_required
def api_toggle_vless_key(server_id, name):
    server, err = _owned_server(server_id)
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    enable = bool(data.get("enable"))
    try:
        client = make_client(server)
        rec = client.toggle_vless_client(name, enable)
        links = client.build_vless_links(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    return jsonify(_vless_action_response(name, rec, links))


@app.route("/api/server/<int:server_id>/vless-clients/<path:name>/gb",
           methods=["POST"])
@login_required
def api_gb_vless_key(server_id, name):
    server, err = _owned_server(server_id)
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
        rec = client.set_gb_vless_client(name, GB_BYTES[gb])
        links = client.build_vless_links(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    HEALTH_CACHE.pop(server_id, None)
    resp = _vless_action_response(name, rec, links)
    resp["gb"] = gb
    return jsonify(resp)


@app.route("/api/server/<int:server_id>/vless-clients/<path:name>/edit",
           methods=["POST"])
@login_required
def api_edit_vless_key(server_id, name):
    server, err = _owned_server(server_id)
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
        rec, new_name = client.edit_vless_client(name, gb_bytes, new_date)
        links = client.build_vless_links(rec)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    if new_name != name:
        note_key_renamed(server_id, name, new_name)
    HEALTH_CACHE.pop(server_id, None)
    resp = _vless_action_response(new_name, rec, links)
    resp["old_name"] = name
    return jsonify(resp)


# ---------------------------------------------------------------- users (v6, Super Admin only)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


@app.route("/api/users")
@login_required
@require_admin
def api_users():
    # Never returns hashes — username / role / display name only.
    return jsonify({"ok": True, "users": list_users()})


# ---------------------------------------------------------------- v6.1.6: presets API
@app.route("/api/presets")
@login_required
def api_my_presets():
    """The logged-in user's own presets (both protocols) — what the
    Add Key / Bulk Create forms render. A user only ever sees their
    own tiers."""
    return jsonify({"ok": True,
                    "presets": get_user_presets(current_user()["username"])})


@app.route("/api/users/<username>/presets")
@login_required
@require_admin
def api_get_user_presets(username):
    if not get_user(username):
        return jsonify({"ok": False, "error": "user not found"}), 404
    return jsonify({"ok": True, "username": username,
                    "presets": get_user_presets(username)})


@app.route("/api/users/<username>/presets", methods=["PUT"])
@login_required
@require_admin
def api_set_user_presets(username):
    if not get_user(username):
        return jsonify({"ok": False, "error": "user not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    # {outline: [...], vless: [...]} or {presets: {outline, vless}}.
    body = data.get("presets") if isinstance(data.get("presets"),
                                             dict) else data
    if "outline" not in body and "vless" not in body:
        return jsonify({"ok": False,
                        "error": "outline and/or vless tiers needed"}), 400
    try:
        set_user_presets(username, body.get("outline"),
                         body.get("vless"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "username": username,
                    "presets": get_user_presets(username)})


@app.route("/api/users", methods=["POST"])
@login_required
@require_admin
def api_add_user():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    display = (data.get("display_name") or "").strip()
    if not USERNAME_RE.match(username):
        return jsonify({"ok": False,
                        "error": "username must be 3-32 chars: letters, "
                                 "digits, _ or -"}), 400
    if len(password) < 6:
        return jsonify({"ok": False,
                        "error": "password must be at least 6 characters"}), 400
    if not display:
        return jsonify({"ok": False, "error": "display name needed"}), 400
    if get_user(username):
        return jsonify({"ok": False,
                        "error": "username already exists"}), 400
    # Role is ALWAYS reseller here — only the setup flow creates an admin.
    create_user(username, password, "reseller", display)
    return jsonify({"ok": True, "username": username})


@app.route("/api/users/<username>", methods=["PUT"])
@login_required
@require_admin
def api_edit_user(username):
    if not get_user(username):
        return jsonify({"ok": False, "error": "user not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    changed = False
    if data.get("password"):
        if len(data["password"]) < 6:
            return jsonify({"ok": False,
                            "error": "password must be at least 6 characters"}), 400
        set_user_password(username, data["password"])
        changed = True
    if (data.get("display_name") or "").strip():
        set_user_display_name(username, data["display_name"].strip())
        changed = True
    if not changed:
        return jsonify({"ok": False, "error": "nothing to change"}), 400
    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["DELETE"])
@login_required
@require_admin
def api_delete_user(username):
    me = current_user()["username"]
    if username == "admin" or username == me:
        return jsonify({"ok": False,
                        "error": "cannot delete the admin or yourself"}), 400
    if not get_user(username):
        return jsonify({"ok": False, "error": "user not found"}), 404
    delete_user(username)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- bulk server import (v6, Super Admin only)
@app.route("/api/servers/import", methods=["POST"])
@login_required
@require_admin
def api_import_servers():
    """Import many servers at once. Body: a JSON array (or
    {"servers": [...]}) of server objects — same fields as one server.
    Duplicates (same base_url + panel_path) are skipped, never
    overwritten. Returns {added, skipped, errors[]}. Passwords are
    NEVER echoed back."""
    data = request.get_json(force=True, silent=True) or {}
    items = data if isinstance(data, list) else data.get("servers")
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False,
                        "error": "send a JSON array of servers"}), 400
    db = get_db()
    existing = {(r["base_url"].rstrip("/"), r["panel_path"])
                for r in db.execute("SELECT base_url, panel_path "
                                    "FROM servers")}
    added, skipped, errors = 0, 0, []
    seen = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"index": i, "error": "not an object"})
            continue
        name = (item.get("name") or "").strip()
        stype = (item.get("type") or "").strip()
        base = (item.get("base_url") or "").strip().rstrip("/")
        path = (item.get("panel_path") or "").strip()
        host = (item.get("share_host") or "").strip()
        if not name or stype not in ("xui", "marzban") or not base or not host:
            errors.append({"index": i, "name": name or None,
                           "error": "name, type (xui/marzban), base_url "
                                    "and share_host are required"})
            continue
        key = (base, path)
        if key in existing or key in seen:
            skipped += 1
            continue
        owner = (item.get("owner") or "admin").strip()
        if owner != "admin" and not get_user(owner):
            errors.append({"index": i, "name": name,
                           "error": "owner is not a known user"})
            continue
        try:
            db.execute(
                """INSERT INTO servers (name, country, type, base_url,
                   panel_path, admin_user, admin_pw_enc, share_host,
                   share_port, is_free, is_sales, sort_order, created_at,
                   owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, (item.get("country") or "").strip(), stype, base,
                 path, (item.get("admin_user") or "kohein").strip(),
                 encrypt_pw(item.get("admin_pw") or ""),
                 host, int(item.get("share_port") or 1080),
                 1 if item.get("is_free") else 0,
                 1 if item.get("is_sales") else 0,
                 int(item.get("sort_order") or 0), int(time.time()), owner))
            seen.add(key)
            added += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"index": i, "name": name,
                           "error": str(exc)[:200]})
    db.commit()
    return jsonify({"ok": True, "added": added, "skipped": skipped,
                    "errors": errors})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
