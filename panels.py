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


import re as _re

_NAME_DATE_RE = _re.compile(r"^(.*?)__(\d{1,2})[_-](\d{1,2})[_-](\d{4})$")

RENEW_DAYS = {1: 30, 2: 60, 3: 90}


def verify_with_retry(get_fn, expect_present=True, tries=3, delay=1):
    """Read a client/user back after a write. The panels can answer a
    read a beat behind a write (the 2026-11-27 v2 test saw renew report
    502 even though the rename had persisted), so retry briefly before
    concluding the record is missing / still there. Returns the record
    (or None) from the last attempt."""
    got = None
    for _ in range(tries):
        got = get_fn()
        if bool(got) == expect_present:
            return got
        time.sleep(delay)
    return got


def parse_name_date(name):
    """Split a client name into (base, expiry_date).

    The name convention is Name__D_M_YYYY (double underscore, no
    zero-padding); older names sometimes use dashes in the date part.
    Returns (base, date) or (name, None) when no date suffix parses.
    """
    m = _NAME_DATE_RE.match(name or "")
    if not m:
        return name, None
    try:
        d = datetime(int(m.group(4)), int(m.group(3)), int(m.group(2))).date()
    except ValueError:
        return name, None
    return m.group(1), d


def renew_name_and_date(name, old_date, months):
    """Renew maths: new date = old date + 30/60/90 days; the new name
    encodes the new date in the same Name__D_M_YYYY form. GB is unchanged
    on renew — only the name and the expiry move."""
    if months not in RENEW_DAYS:
        raise PanelError("months must be 1, 2 or 3")
    new_date = old_date + timedelta(days=RENEW_DAYS[months])
    base, _ = parse_name_date(name)
    new_name = "%s__%d_%d_%d" % (base, new_date.day, new_date.month,
                                 new_date.year)
    return new_name, new_date


def rename_to_date(name, new_date):
    """New name encoding new_date in the Name__D_M_YYYY convention."""
    base, _ = parse_name_date(name)
    return "%s__%d_%d_%d" % (base, new_date.day, new_date.month,
                             new_date.year)


def xui_expiry_ms(new_date):
    """3X-UI expiryTime for a named date: +1 day 00:00 Myanmar, ms."""
    return int((datetime(new_date.year, new_date.month, new_date.day,
                         tzinfo=MM) + timedelta(days=1)).timestamp() * 1000)


def marzban_expiry_s(new_date):
    """Marzban expire for a named date: 23:59 Myanmar, unix seconds."""
    return int(datetime(new_date.year, new_date.month, new_date.day,
                       23, 59, tzinfo=MM).timestamp())


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

    def _raw_client(self, email):
        """The raw client dict straight from inbound 1 settings (keeps
        id, flow, limitIp, tgId, subId, comment, reset — everything the
        update endpoint expects back), or None."""
        r = self._request("GET", "/panel/api/inbounds/get/1")
        try:
            obj = r.json().get("obj", {})
        except Exception:
            return None
        settings = obj.get("settings", {})
        if isinstance(settings, str):
            settings = json.loads(settings)
        for c in settings.get("clients", []) or []:
            if c.get("email") == email:
                return dict(c)
        return None

    def _client_id(self, email):
        """The client's numeric id, from /panel/api/clients/get (the
        inbound settings records carry NO id). Returns str(id) or None."""
        r = self._request("GET", "/panel/api/clients/get/" +
                          urllib.parse.quote(email))
        try:
            obj = r.json().get("obj") or {}
            cid = (obj.get("client") or {}).get("id")
        except Exception:
            cid = None
        return str(cid) if cid is not None else None

    def update_client(self, old_email, **changes):
        """Update one client with the bare client object. Mirrors
        tg_xui_rename.py: the FULL raw record from inbound settings is
        sent back, only the changed fields differ — plus the `id`, which
        the settings record does NOT carry, so it is read from
        /panel/api/clients/get and sent as a STRING (a number fails)."""
        raw = self._raw_client(old_email)
        if not raw:
            raise PanelError("update: client not found: %s" % old_email)
        cid = self._client_id(old_email)
        if cid is not None:
            raw["id"] = cid
        raw.update(changes)
        r = self._request("POST", "/panel/api/clients/update/" +
                          urllib.parse.quote(old_email),
                          json=raw,
                          headers={"Content-Type": "application/json"})
        try:
            res = r.json()
        except Exception:
            raise PanelError("update: bad response from panel")
        if not res.get("success"):
            raise PanelError("update failed: %s" % res.get("msg", r.text[:200]))
        new_email = changes.get("email", old_email)
        got = verify_with_retry(lambda: self.get_client(new_email))
        if not got:
            raise PanelError("update: client not found on panel after update")
        return got

    def renew_client(self, email, months):
        """Renew: new expiry = old expiry + 30/60/90 days; the client is
        RENAMED to the new date (the name encodes the expiry) and the
        expiryTime moves in the same update call. GB is unchanged.
        Returns (new_record, new_name)."""
        cur = self.get_client(email)
        if not cur:
            raise PanelError("renew: client not found: %s" % email)
        _base, old_date = parse_name_date(email)
        if old_date is None:
            # fall back to the stored expiryTime (named date + 1 day 00:00 MM)
            if not cur.get("expiry"):
                raise PanelError("renew: no expiry date on %s" % email)
            old_date = (datetime.fromtimestamp(cur["expiry"], MM)
                        - timedelta(days=1)).date()
        new_name, new_date = renew_name_and_date(email, old_date, months)
        new_exp_ms = int((datetime(new_date.year, new_date.month,
                                   new_date.day, tzinfo=MM)
                          + timedelta(days=1)).timestamp() * 1000)
        rec = self.update_client(email, email=new_name, expiryTime=new_exp_ms)
        return rec, new_name

    def swap_client(self, email):
        """Swap: DELETE the old client first, then create a fresh one with
        the SAME name / expiry / GB but a fresh panel-generated password.
        Used when a key does not connect. Returns the new record."""
        cur = self.get_client(email)
        if not cur:
            raise PanelError("swap: client not found: %s" % email)
        was_enabled = bool(cur.get("enable", True))
        self.delete_client(email)
        try:
            rec = self.create_client(email, cur["limit"] or 0,
                                     int(cur["expiry"] * 1000) if cur.get("expiry") else 0)
        except Exception:
            raise PanelError("swap: old key deleted but recreate failed — "
                             "recreate %s manually on the panel" % email)
        if not was_enabled:
            rec = self.update_client(email, enable=False)
        return rec

    def toggle_client(self, email, enable):
        return self.update_client(email, enable=bool(enable))

    def set_gb_client(self, email, total_gb_bytes):
        return self.update_client(email, totalGB=int(total_gb_bytes))

    def edit_client(self, email, gb_bytes=None, new_date=None):
        """Edit GB limit and/or expiry date (v3). A date change RENAMES
        the key to the new date (the name encodes the expiry) in the SAME
        update call as the expiry move; a GB-only change does not rename.
        Returns (new_record, new_name)."""
        cur = self.get_client(email)
        if not cur:
            raise PanelError("edit: client not found: %s" % email)
        changes = {}
        new_name = email
        if new_date is not None:
            _base, old_date = parse_name_date(email)
            if old_date is None:
                if not cur.get("expiry"):
                    raise PanelError("edit: no expiry date on %s" % email)
                old_date = (datetime.fromtimestamp(cur["expiry"], MM)
                            - timedelta(days=1)).date()
            if new_date != old_date:
                new_name = rename_to_date(email, new_date)
                changes["email"] = new_name
                changes["expiryTime"] = xui_expiry_ms(new_date)
        if gb_bytes is not None:
            changes["totalGB"] = int(gb_bytes)
        if not changes:
            return cur, email
        rec = self.update_client(email, **changes)
        return rec, new_name

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
        if verify_with_retry(lambda: self.get_client(email),
                             expect_present=False) is not None:
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
        """Login + list. Returns (online, client_count, used_total_bytes,
        limit_total_bytes) — the limit total feeds the dashboard usage bar."""
        clients = self.list_clients()
        used = sum(c["used"] for c in clients)
        limit = sum(c["limit"] or 0 for c in clients)
        return True, len(clients), used, limit


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

    def update_user(self, old_username, **fields):
        """Partial update via PUT /api/user/{old_username} (mirrors the
        password-PUT in create_user). Accepts username (rename), expire,
        data_limit, status, proxies. Returns the verified record.
        NOTE: the first parameter is named old_username (not username)
        because renew passes username=<new name> as a field — passing
        both positionally and as a keyword raises TypeError."""
        r = self._request("PUT", "/api/user/" + urllib.parse.quote(old_username),
                          json=fields)
        if r.status_code >= 400:
            raise PanelError("update failed: %s" % r.text[:200])
        new_name = fields.get("username", old_username)
        got = verify_with_retry(lambda: self.get_user(new_name))
        if not got:
            raise PanelError("update: user not found on panel after update")
        return got

    def renew_user(self, username, months):
        """Renew: new expiry = old expiry + 30/60/90 days; the user is
        RENAMED to the new date (the name encodes the expiry) and the
        expiry moves with it. GB limit is unchanged.
        Marzban 0.8.4 IGNORES a username field in PUT /api/user (verified
        live 2026-11-27: PUT returns 200 echoing the OLD name), so a
        rename is done as DELETE + RECREATE with the new name — same
        pattern as swap_user / tg_marzban_recreate.py. The fresh user
        starts with used=0 (a renewal = a fresh allowance period).
        Returns (new_record, new_name)."""
        cur = self.get_user(username)
        if not cur:
            raise PanelError("renew: user not found: %s" % username)
        _base, old_date = parse_name_date(username)
        if old_date is None:
            if not cur.get("expiry"):
                raise PanelError("renew: no expiry date on %s" % username)
            old_date = datetime.fromtimestamp(cur["expiry"], MM).date()
        new_name, new_date = renew_name_and_date(username, old_date, months)
        new_exp = int(datetime(new_date.year, new_date.month, new_date.day,
                               23, 59, tzinfo=MM).timestamp())
        was_enabled = bool(cur.get("enable", True))
        self.delete_user(username)
        try:
            rec = self.create_user(new_name, cur["limit"] or 0, new_exp)
        except Exception:
            raise PanelError("renew: old key deleted but recreate failed — "
                             "recreate %s manually on the panel" % new_name)
        if not was_enabled:
            rec = self.update_user(new_name, status="disabled")
        return rec, new_name

    def swap_user(self, username):
        """Swap: DELETE the old user first, then create a fresh one with
        the SAME name / expiry / GB but a fresh random password (the
        create path PUTs the explicit password, per the hard-won lesson).
        Returns the new record."""
        cur = self.get_user(username)
        if not cur:
            raise PanelError("swap: user not found: %s" % username)
        was_enabled = bool(cur.get("enable", True))
        self.delete_user(username)
        try:
            rec = self.create_user(username, cur["limit"] or 0,
                                   cur["expiry"] or 0)
        except Exception:
            raise PanelError("swap: old key deleted but recreate failed — "
                             "recreate %s manually on the panel" % username)
        if not was_enabled:
            rec = self.update_user(username, status="disabled")
        return rec

    def toggle_user(self, username, enable):
        return self.update_user(username,
                                status="active" if enable else "disabled")

    def set_gb_user(self, username, total_gb_bytes):
        return self.update_user(username, data_limit=int(total_gb_bytes))

    def edit_user(self, username, gb_bytes=None, new_date=None):
        """Edit GB limit and/or expiry date (v3). A date change RENAMES
        the user — Marzban PUT ignores username, so rename = DELETE +
        RECREATE with the new name (same pattern as renew_user; the fresh
        user starts with used=0). A GB-only change is a plain PUT and
        keeps the name, password and used traffic. Returns
        (new_record, new_name)."""
        cur = self.get_user(username)
        if not cur:
            raise PanelError("edit: user not found: %s" % username)
        new_name = username
        if new_date is not None:
            _base, old_date = parse_name_date(username)
            if old_date is None:
                if not cur.get("expiry"):
                    raise PanelError("edit: no expiry date on %s" % username)
                old_date = datetime.fromtimestamp(cur["expiry"], MM).date()
            if new_date != old_date:
                new_name = rename_to_date(username, new_date)
        if new_name != username:
            new_exp = marzban_expiry_s(new_date)
            new_limit = (int(gb_bytes) if gb_bytes is not None
                         else (cur["limit"] or 0))
            was_enabled = bool(cur.get("enable", True))
            self.delete_user(username)
            try:
                rec = self.create_user(new_name, new_limit, new_exp)
            except Exception:
                raise PanelError("edit: old key deleted but recreate failed — "
                                 "recreate %s manually on the panel" % new_name)
            if not was_enabled:
                rec = self.update_user(new_name, status="disabled")
            return rec, new_name
        if gb_bytes is not None:
            rec = self.update_user(username, data_limit=int(gb_bytes))
            return rec, username
        return cur, username

    def delete_user(self, username):
        r = self._request("DELETE", "/api/user/" + urllib.parse.quote(username))
        if r.status_code == 404:
            return True  # already gone
        if r.status_code >= 400:
            raise PanelError("delete failed: %s" % r.text[:200])
        if verify_with_retry(lambda: self.get_user(username),
                             expect_present=False) is not None:
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
        limit = sum(u["limit"] or 0 for u in users)
        return True, len(users), used, limit


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
