#!/usr/bin/env python3
"""Panel clients for KHP Panel v1.

Talks to the user's real servers exactly the way the proven scripts in
~/workspace/tools/telegram/ do — same endpoints, same payloads, same
lessons (see SPEC.md). No guessing: every call here mirrors a working
script (tg_xui_login.py, tg_xui_key.py, tg_xui_del.py, tg_marzban_login.py,
tg_marzban_key.py, tg_marzban_list.py, tg_marzban_del.py).

Security: panel passwords are NEVER logged. Full ss:// links are built
only in memory and returned to the logged-in admin's browser.
"""
import base64
import json
import secrets
import string
import time
import urllib.parse
import urllib3
from datetime import datetime, timedelta, timezone

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MM = timezone(timedelta(hours=6, minutes=30))

GB_BYTES = {
    400: 429496729600,
    500: 536870912000,
    1000: 1099511627776,
    1500: 1649267441664,
}
MONTHS_GB = {1: 400, 2: 1000, 3: 1500}

# connect timeout, read timeout (spec)
TIMEOUT = (5, 20)


# ---------------------------------------------------------------- helpers
def add_months(date, months):
    """Calendar months, e.g. 2026-11-27 + 1 -> 2026-12-27."""
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    day = min(date.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date.replace(year=year, month=month, day=day)


def expiry_for(months, today=None):
    """Return (expiry_date, name_suffix) for a key bought today.

    Name suffix is the expiry date, no zero-padding: D_M_YYYY.
    """
    today = today or datetime.now(MM).date()
    exp = add_months(today, months)
    return exp, "%d_%d_%d" % (exp.day, exp.month, exp.year)


def gb_to_bytes(gb):
    return GB_BYTES[gb]


def fmt_gb(nbytes):
    """Bytes -> short string, e.g. 12.4 GB / 856 MB / 0 B."""
    if nbytes is None:
        return "—"
    if nbytes >= 1073741824:
        return "%.1f GB" % (nbytes / 1073741824)
    if nbytes >= 1048576:
        return "%.0f MB" % (nbytes / 1048576)
    if nbytes >= 1024:
        return "%.0f KB" % (nbytes / 1024)
    return "%d B" % nbytes


def ss_b64(method, password):
    """base64(method:password) with NO '=' padding — the panel convention."""
    raw = base64.b64encode(("%s:%s" % (method, password)).encode()).decode()
    return raw.rstrip("=")


def client_status(enable, expiry_unix, used, limit):
    """Shared status: active / expired / disabled / over-limit."""
    now = time.time()
    if expiry_unix and expiry_unix < now:
        return "expired"
    if not enable:
        return "disabled"
    if limit and limit > 0 and used is not None and used >= limit:
        return "over-limit"
    return "active"


class PanelError(Exception):
    pass


# ---------------------------------------------------------------- 3X-UI
class XuiClient:
    """3X-UI panel 3.3.1. Mirrors tg_xui_login.py / tg_xui_key.py / tg_xui_del.py."""

    def __init__(self, base_url, panel_path, username, password,
                 share_host, share_port):
        self.url = base_url.rstrip("/") + "/" + panel_path.strip("/")
        self.username = username
        self.password = password
        self.share_host = share_host
        self.share_port = int(share_port)
        self.session = requests.Session()
        self.csrf = None

    def login(self):
        s = self.session
        s.get(self.url + "/", timeout=TIMEOUT, verify=False)
        r = s.get(self.url + "/csrf-token", timeout=TIMEOUT, verify=False)
        try:
            self.csrf = r.json().get("obj", "")
        except Exception:
            self.csrf = ""
        if not self.csrf:
            raise PanelError("no CSRF token from panel")
        r = s.post(self.url + "/login",
                   headers={"X-CSRF-Token": self.csrf},
                   data={"username": self.username,
                         "password": self.password},
                   timeout=TIMEOUT, verify=False)
        try:
            ok = r.json().get("success", False)
        except Exception:
            ok = False
        if not ok:
            raise PanelError("panel login failed (bad username/password?)")

    def _headers(self):
        return {"X-CSRF-Token": self.csrf or ""}

    def _request(self, method, path, retry=True, **kwargs):
        """One API call; re-login once if the session expired."""
        if not self.csrf:
            self.login()
        kwargs.setdefault("timeout", TIMEOUT)
        kwargs.setdefault("verify", False)
        headers = dict(self._headers())
        extra = kwargs.pop("headers", None)
        if extra:
            headers.update(extra)
        r = self.session.request(method, self.url + path,
                                 headers=headers, **kwargs)
        if retry and r.status_code in (401, 403):
            self.login()
            headers = dict(self._headers())
            if extra:
                headers.update(extra)
            r = self.session.request(method, self.url + path,
                                     headers=headers, **kwargs)
        return r

    def list_clients(self):
        """All Outline (inbound 1) clients, normalised.

        Usage = MAX(up+down) across inbounds per client — the same global
        counter is copied into every inbound, NEVER sum it.
        """
        r = self._request("GET", "/panel/api/inbounds/list")
        data = r.json()
        inbounds = data.get("obj", [])
        usage = {}  # email -> max(up+down); also id -> max
        outline = None
        for inb in inbounds:
            for st in inb.get("clientStats", []) or []:
                total = (st.get("up") or 0) + (st.get("down") or 0)
                for key in (st.get("email"), st.get("id")):
                    if key:
                        usage[key] = max(usage.get(key, 0), total)
            if inb.get("id") == 1:
                outline = inb
        if outline is None:
            # fall back to the first shadowsocks inbound
            for inb in inbounds:
                if inb.get("protocol") == "shadowsocks":
                    outline = inb
                    break
        if outline is None:
            raise PanelError("no Outline/shadowsocks inbound found")
        settings = outline.get("settings", {})
        if isinstance(settings, str):
            settings = json.loads(settings)
        clients = []
        for c in settings.get("clients", []) or []:
            email = c.get("email", "")
            used = usage.get(email)
            if used is None:
                used = usage.get(c.get("id"), 0)
            total_gb = c.get("totalGB") or 0
            expiry_ms = c.get("expiryTime") or 0
            expiry = expiry_ms / 1000 if expiry_ms else 0
            clients.append({
                "name": email,
                "id": c.get("id"),
                "enable": bool(c.get("enable", True)),
                "used": used or 0,
                "limit": total_gb,
                "expiry": expiry,
                "status": client_status(bool(c.get("enable", True)),
                                        expiry, used or 0, total_gb),
                "method": c.get("method", "chacha20-ietf-poly1305"),
                "password": c.get("password", ""),
            })
        return clients

    def get_client(self, email):
        """True record incl. usedTraffic, or None if not found."""
        r = self._request("GET", "/panel/api/clients/get/" +
                          urllib.parse.quote(email))
        try:
            data = r.json()
        except Exception:
            return None
        if "record not found" in r.text:
            return None
        obj = data.get("obj") or {}
        client = obj.get("client") or obj
        if not client or not client.get("email"):
            return None
        expiry_ms = client.get("expiryTime") or 0
        expiry = expiry_ms / 1000 if expiry_ms else 0
        # usedTraffic lives on obj (sibling of client), not inside client
        used = obj.get("usedTraffic")
        if used is None:
            used = client.get("usedTraffic") or 0
        return {
            "name": client.get("email", email),
            "enable": bool(client.get("enable", True)),
            "used": used,
            "limit": client.get("totalGB") or 0,
            "expiry": expiry,
            "status": client_status(bool(client.get("enable", True)),
                                    expiry, used, client.get("totalGB") or 0),
            "method": client.get("method", "chacha20-ietf-poly1305"),
            "password": client.get("password", ""),
        }

    def create_client(self, name, total_gb_bytes, expiry_ms):
        """Create on inbound 1, then GET it back to read the password."""
        payload = {"client": {"email": name, "totalGB": total_gb_bytes,
                              "expiryTime": expiry_ms, "enable": True},
                   "inboundIds": [1]}
        r = self._request("POST", "/panel/api/clients/add",
                          json=payload,
                          headers={"Content-Type": "application/json"})
        try:
            res = r.json()
        except Exception:
            raise PanelError("create: bad response from panel")
        if not res.get("success"):
            raise PanelError("create failed: %s" % res.get("msg", r.text[:200]))
        # verify: read the client back from inbound 1 (password is generated)
        r = self._request("GET", "/panel/api/inbounds/get/1")
        obj = r.json().get("obj", {})
        settings = obj.get("settings", {})
        if isinstance(settings, str):
            settings = json.loads(settings)
        for c in settings.get("clients", []) or []:
            if c.get("email") == name:
                expiry_ms2 = c.get("expiryTime") or 0
                return {
                    "name": name,
                    "enable": bool(c.get("enable", True)),
                    "used": 0,
                    "limit": c.get("totalGB") or 0,
                    "expiry": expiry_ms2 / 1000 if expiry_ms2 else 0,
                    "status": "active",
                    "method": c.get("method", "chacha20-ietf-poly1305"),
                    "password": c.get("password", ""),
                }
        raise PanelError("create: client not found on panel after add")

    def delete_client(self, email):
        r = self._request("POST", "/panel/api/clients/del/" +
                          urllib.parse.quote(email))
        try:
            res = r.json()
            if not res.get("success"):
                raise PanelError("delete failed: %s" % res.get("msg", r.text[:200]))
        except PanelError:
            raise
        except Exception:
            pass  # some panels return non-JSON; verify below instead
        if self.get_client(email) is not None:
            raise PanelError("delete: client still on panel after delete")
        return True

    def build_link(self, client):
        method = client.get("method") or "chacha20-ietf-poly1305"
        password = client.get("password") or ""
        if not password:
            raise PanelError("no password on record for this client")
        frag = urllib.parse.quote(client["name"], safe="")
        return "ss://%s@%s:%d?type=tcp#%s" % (
            ss_b64(method, password), self.share_host, self.share_port, frag)

    def health(self):
        """Login + list. Returns (online, client_count, used_total_bytes)."""
        clients = self.list_clients()
        used = sum(c["used"] for c in clients)
        return True, len(clients), used


# ---------------------------------------------------------------- Marzban
class MarzbanClient:
    """Marzban 0.8.4. Mirrors tg_marzban_login.py / tg_marzban_key.py /
    tg_marzban_list.py / tg_marzban_del.py."""

    def __init__(self, base_url, username, password, share_host, share_port):
        self.url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.share_host = share_host
        self.share_port = int(share_port)
        self.token = None

    def login(self):
        r = requests.post(self.url + "/api/admin/token",
                          data={"username": self.username,
                                "password": self.password},
                          timeout=TIMEOUT)
        try:
            self.token = r.json()["access_token"]
        except Exception:
            raise PanelError("marzban login failed (bad username/password?)")

    def _headers(self):
        return {"Authorization": "Bearer " + (self.token or "")}

    def _request(self, method, path, retry=True, **kwargs):
        if not self.token:
            self.login()
        kwargs.setdefault("timeout", TIMEOUT)
        headers = dict(self._headers())
        extra = kwargs.pop("headers", None)
        if extra:
            headers.update(extra)
        r = requests.request(method, self.url + path,
                             headers=headers, **kwargs)
        if retry and r.status_code == 401:
            self.login()
            headers = dict(self._headers())
            if extra:
                headers.update(extra)
            r = requests.request(method, self.url + path,
                                 headers=headers, **kwargs)
        return r

    def list_users(self):
        r = self._request("GET", "/api/users")
        data = r.json()
        users = data.get("users", [])
        out = []
        for u in users:
            expire = u.get("expire") or 0
            used = u.get("used_traffic") or 0
            limit = u.get("data_limit") or 0
            status = u.get("status") or "active"
            enable = status not in ("disabled", "expired")
            st = status
            if expire and expire < time.time():
                st = "expired"
            elif status == "disabled":
                st = "disabled"
            elif limit and used >= limit:
                st = "over-limit"
            elif status == "limited":
                st = "over-limit"
            else:
                st = "active"
            pw = ""
            meth = "chacha20-ietf-poly1305"
            try:
                pw = u["proxies"]["shadowsocks"].get("password", "")
                meth = u["proxies"]["shadowsocks"].get("method", meth)
            except Exception:
                pass
            out.append({
                "name": u.get("username", ""),
                "enable": enable,
                "used": used,
                "limit": limit,
                "expiry": expire,
                "status": st,
                "method": meth,
                "password": pw,
            })
        return out

    def get_user(self, username):
        r = self._request("GET", "/api/user/" + urllib.parse.quote(username))
        if r.status_code == 404:
            return None
        u = r.json()
        if not u or not u.get("username"):
            return None
        expire = u.get("expire") or 0
        used = u.get("used_traffic") or 0
        limit = u.get("data_limit") or 0
        pw, meth = "", "chacha20-ietf-poly1305"
        try:
            pw = u["proxies"]["shadowsocks"].get("password", "")
            meth = u["proxies"]["shadowsocks"].get("method", meth)
        except Exception:
            pass
        return {
            "name": u.get("username", username),
            "enable": u.get("status") not in ("disabled", "expired"),
            "used": used,
            "limit": limit,
            "expiry": expire,
            "status": client_status(u.get("status") not in ("disabled", "expired"),
                                    expire, used, limit),
            "method": meth,
            "password": pw,
        }

    def create_user(self, name, total_gb_bytes, expire_unix):
        """Create + PUT explicit password (auto-generated passwords are NOT
        applied to the running Xray core) + GET back for the stored password."""
        alphabet = string.ascii_lowercase + string.digits
        user_pw = "".join(secrets.choice(alphabet) for _ in range(16))
        payload = {"username": name,
                   "proxies": {"shadowsocks": {"password": user_pw}},
                   "inbounds": {"shadowsocks": ["Shadowsocks TCP"]},
                   "expire": expire_unix, "data_limit": total_gb_bytes,
                   "status": "active"}
        r = self._request("POST", "/api/user", json=payload)
        if r.status_code >= 400:
            raise PanelError("create failed: %s" % r.text[:200])
        # hard-won lesson: PUT the explicit password so the core applies it
        put = {"proxies": {"shadowsocks": {"password": user_pw}},
               "status": "active"}
        r2 = self._request("PUT", "/api/user/" + urllib.parse.quote(name),
                           json=put)
        if r2.status_code >= 400:
            raise PanelError("create: password PUT failed: %s" % r2.text[:200])
        got = self.get_user(name)
        if not got:
            raise PanelError("create: user not found on panel after add")
        return got

    def delete_user(self, username):
        r = self._request("DELETE", "/api/user/" + urllib.parse.quote(username))
        if r.status_code == 404:
            return True  # already gone
        if r.status_code >= 400:
            raise PanelError("delete failed: %s" % r.text[:200])
        if self.get_user(username) is not None:
            raise PanelError("delete: user still on panel after delete")
        return True

    def build_link(self, user):
        method = user.get("method") or "chacha20-ietf-poly1305"
        password = user.get("password") or ""
        if not password:
            raise PanelError("no password on record for this user")
        # Marzban links have NO ?type= query
        frag = urllib.parse.quote(user["name"], safe="")
        return "ss://%s@%s:%d#%s" % (
            ss_b64(method, password), self.share_host, self.share_port, frag)

    def health(self):
        users = self.list_users()
        used = sum(u["used"] for u in users)
        return True, len(users), used


def make_client(server):
    """Build a panel client from a server DB row (dict)."""
    if server["type"] == "xui":
        return XuiClient(server["base_url"], server.get("panel_path") or "",
                         server["admin_user"], server["admin_pw"],
                         server["share_host"], server["share_port"])
    if server["type"] == "marzban":
        return MarzbanClient(server["base_url"], server["admin_user"],
                             server["admin_pw"], server["share_host"],
                             server["share_port"])
    raise PanelError("unknown server type: %s" % server["type"])
