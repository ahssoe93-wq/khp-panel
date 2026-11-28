#!/usr/bin/env python3
"""Panel clients for KHP Panel v6.

Talks to the user's real servers exactly the way the proven scripts in
~/workspace/tools/telegram/ do — same endpoints, same payloads, same
lessons (see SPEC.md). No guessing: every call here mirrors a working
script (tg_xui_login.py, tg_xui_key.py, tg_xui_del.py, tg_marzban_login.py,
tg_marzban_key.py, tg_marzban_list.py, tg_marzban_del.py).

v6: inbounds are detected by PROTOCOL at runtime (never hardcode id 1);
VLESS (3X-UI only) is managed alongside Outline — one UUID on every
vless inbound, links built from streamSettings. Marzban VLESS is NOT
managed (reported only, by the seed probe).

Security: panel passwords are NEVER logged. Full ss:// / vless:// links
are built only in memory and returned to the logged-in user.
"""
import base64
import json
import secrets
import string
import time
import uuid
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

# v6.1: how fresh a Marzban user's online_at must be to count as
# "currently online" (seconds). Marzban has no "currently connected"
# endpoint — online_at is the last time the node reported the user
# online, refreshed while they stay connected. 5 minutes matches the
# recency the Marzban admin UI itself uses.
ONLINE_WINDOW_SECS = 300


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

RENEW_DAYS = {1: 30, 2: 60, 3: 90, 4: 120, 5: 150, 6: 180}


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
        raise PanelError("months must be 1..6")
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


def _as_dict(v):
    """A JSON field that may arrive as a dict or a JSON string."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}


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


def merge_xui_clients(outline, vless):
    """v6.1.5: one record per customer (email) across Outline + VLESS.

    A customer who holds BOTH an Outline key and a VLESS key on the
    same server used to count as TWO keys everywhere (dashboard totals,
    server rows, expiring/expired/low lists, online count) — e.g.
    STH-PREMIUM10 showed 200 for 100 real customers. Merge rule (same
    as the existing VLESS dedup across vless inbounds): expiry = max,
    limit = max, used = max, enable = AND, status from the merged
    values. The merged record keeps both sub-records ("outline" /
    "vless") plus both identities (Outline "id", VLESS "uuid") so key
    actions can reach both sides, and "protocol" is "both", "outline"
    or "vless" (with "protocols" listing the sides present). Order:
    Outline order first, then VLESS-only names in VLESS order."""
    by_name = {}
    order = []
    for c in outline or []:
        name = c.get("name") or ""
        if not name:
            continue
        if name not in by_name:
            by_name[name] = {"outline": None, "vless": None}
            order.append(name)
        by_name[name]["outline"] = c
    for c in vless or []:
        name = c.get("name") or ""
        if not name:
            continue
        if name not in by_name:
            by_name[name] = {"outline": None, "vless": None}
            order.append(name)
        by_name[name]["vless"] = c
    merged = []
    for name in order:
        ol = by_name[name]["outline"]
        vl = by_name[name]["vless"]
        sides = [s for s in (ol, vl) if s is not None]
        enable = all(bool(s.get("enable", True)) for s in sides)
        used = max((s.get("used") or 0) for s in sides)
        limit = max((s.get("limit") or 0) for s in sides)
        expiry = max((s.get("expiry") or 0) for s in sides)
        online = None
        if any(s.get("online") is True for s in sides):
            online = True
        elif any(s.get("online") is False for s in sides):
            online = False
        rec = {
            "name": name,
            "enable": enable,
            "used": used,
            "limit": limit,
            "expiry": expiry,
            "status": client_status(enable, expiry, used, limit),
            "protocol": ("both" if (ol and vl) else
                         "outline" if ol else "vless"),
            "protocols": (["outline"] if ol else []) +
                         (["vless"] if vl else []),
            "outline": ol,
            "vless": vl,
            "id": (ol or {}).get("id"),
            "uuid": (vl or {}).get("uuid"),
            "method": (ol or {}).get("method",
                                     "chacha20-ietf-poly1305"),
            "password": (ol or {}).get("password", ""),
        }
        if online is not None:
            rec["online"] = online
        merged.append(rec)
    return merged


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
        self._inbounds_cache = None  # raw /panel/api/inbounds/list result

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

    def list_inbounds_raw(self, refresh=False):
        """Raw inbounds list from the panel (cached per instance; a write
        invalidates it)."""
        if self._inbounds_cache is None or refresh:
            r = self._request("GET", "/panel/api/inbounds/list")
            data = r.json()
            self._inbounds_cache = data.get("obj", []) or []
        return self._inbounds_cache

    def _usage_map(self, inbounds):
        """email/id -> max(up+down). The same global counter is copied
        into every inbound, so MAX across inbounds — NEVER sum."""
        usage = {}
        for inb in inbounds:
            for st in inb.get("clientStats", []) or []:
                total = (st.get("up") or 0) + (st.get("down") or 0)
                for key in (st.get("email"), st.get("id")):
                    if key:
                        usage[key] = max(usage.get(key, 0), total)
        return usage

    def detect_inbounds(self, refresh=False):
        """Detect inbounds by PROTOCOL at runtime — NEVER assume id 1.
        (v6 lesson: MK-2's Outline is id 3, STH-th45's is id 4, and MK-1
        has NO Outline inbound at all.)
        Returns {"outline": {"id", "port"} | None,
                 "vless": [{"id", "port", "network", "security", "host",
                            "path", "server_name", "mode", "fingerprint"}]}.
        Outline = the first shadowsocks inbound. VLESS = every vless
        inbound (WS on 443 + XHTTP on 2087 in the studied panels).
        v6.1.7: a vless inbound whose shareAddrStrategy is "custom" with
        a non-empty shareAddr reports that address as BOTH host and
        server_name — the address 3X-UI itself puts in its share links."""
        inbounds = self.list_inbounds_raw(refresh=refresh)
        outline = None
        vless = []
        for inb in inbounds:
            proto = (inb.get("protocol") or "").lower()
            if proto == "shadowsocks" and outline is None:
                outline = {"id": inb.get("id"), "port": inb.get("port")}
            elif proto == "vless":
                ss = _as_dict(inb.get("streamSettings"))
                network = (ss.get("network") or "").lower()
                security = (ss.get("security") or "").lower()
                tls = _as_dict(ss.get("tlsSettings"))
                server_name = tls.get("serverName") or ""
                fingerprint = _as_dict(tls.get("settings")).get("fingerprint") or ""
                host, path, mode = "", "", None
                if network == "ws":
                    ws = _as_dict(ss.get("wsSettings"))
                    host = ws.get("host") or server_name
                    path = ws.get("path") or ""
                elif network == "xhttp":
                    xh = _as_dict(ss.get("xhttpSettings"))
                    host = xh.get("host") or server_name
                    path = xh.get("path") or ""
                    mode = xh.get("mode") or "auto"
                # v6.1.7: a VLESS inbound with a Custom share address
                # (3X-UI "Share address strategy" = Custom) exports its
                # links under that address — mirror it here, so the panel
                # builds the same links 3X-UI itself would. Applies to
                # ANY server with a custom share address (today only
                # MK-T157-186: myproxy.giize.com). Any other strategy, or
                # an empty address, leaves the streamSettings host alone.
                share_strategy = str(
                    inb.get("shareAddrStrategy") or "").strip().lower()
                share_addr = str(inb.get("shareAddr") or "").strip()
                if share_strategy == "custom" and share_addr:
                    host = share_addr
                    server_name = share_addr
                vless.append({
                    "id": inb.get("id"), "port": inb.get("port"),
                    "network": network, "security": security,
                    "host": host, "path": path,
                    "server_name": server_name, "mode": mode,
                    "fingerprint": fingerprint,
                })
        return {"outline": outline, "vless": vless}

    def list_clients(self):
        """Outline clients, normalised. The Outline inbound is detected
        by protocol (first shadowsocks inbound) — a server with no
        Outline inbound (VLESS-only) returns [] instead of raising.
        Usage = MAX(up+down) across inbounds per client."""
        inbounds = self.list_inbounds_raw()
        usage = self._usage_map(inbounds)
        ol = self.detect_inbounds()["outline"]
        if ol is None:
            return []
        outline = None
        for inb in inbounds:
            if inb.get("id") == ol["id"]:
                outline = inb
                break
        if outline is None:
            return []
        settings = _as_dict(outline.get("settings"))
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

    def online_emails(self):
        """Emails currently connected, from POST /panel/api/clients/onlines
        (verified live on SG-Gift: {"success": true, "obj": ["email", ...]}).
        Returns None when the endpoint cannot be read — callers must then
        OMIT the online pill (unknown), never mark everyone Offline."""
        try:
            r = self._request("POST", "/panel/api/clients/onlines")
            data = r.json()
            obj = data.get("obj") if isinstance(data, dict) else None
            if isinstance(obj, list):
                return {e for e in obj if isinstance(e, str)}
        except Exception:
            pass
        return None

    def client_ips(self, email):
        """v6.1.3: distinct IPs a client has connected from, from POST
        /panel/api/clients/ips/{email} (GET 404s — POST only). Returns
        {"count": n, "ips": [...]} or None when the endpoint cannot be
        read. The panel answers {"success": true, "obj": ["IP
        (YYYY-MM-DD HH:MM:SS)", ...]} — the IP is the first token."""
        try:
            r = self._request("POST", "/panel/api/clients/ips/" +
                              urllib.parse.quote(email))
            data = r.json()
            obj = data.get("obj") if isinstance(data, dict) else None
            if not isinstance(obj, list):
                return None
            ips = sorted({str(e).split()[0] for e in obj if str(e).split()})
            return {"count": len(ips), "ips": ips}
        except Exception:
            return None

    def list_vless_clients(self):
        """VLESS clients, deduped by UUID across all vless inbounds.
        One customer key = the SAME UUID client on every vless inbound
        (identical email/expiry/totalGB) — so the list merges them into
        one record per UUID. Usage = MAX(up+down) across inbounds, the
        same rule as Outline (the counter is copied, never summed)."""
        inbounds = self.list_inbounds_raw()
        usage = self._usage_map(inbounds)
        vl = self.detect_inbounds()["vless"]
        if not vl:
            return []
        by_id = {inb.get("id"): inb for inb in inbounds}
        merged = {}
        order = []
        for v in vl:
            inb = by_id.get(v["id"])
            if not inb:
                continue
            settings = _as_dict(inb.get("settings"))
            for c in settings.get("clients", []) or []:
                uid = c.get("id") or ""
                email = c.get("email", "")
                if not uid:
                    continue
                used = usage.get(email)
                if used is None:
                    used = usage.get(uid, 0)
                total_gb = c.get("totalGB") or 0
                expiry_ms = c.get("expiryTime") or 0
                expiry = expiry_ms / 1000 if expiry_ms else 0
                enable = bool(c.get("enable", True))
                rec = merged.get(uid)
                if rec is None:
                    rec = {
                        "name": email, "uuid": uid, "protocol": "vless",
                        "enable": enable, "used": used or 0,
                        "limit": total_gb, "expiry": expiry,
                        "status": client_status(enable, expiry,
                                                used or 0, total_gb),
                        "inbounds": [v["id"]],
                    }
                    merged[uid] = rec
                    order.append(uid)
                else:
                    rec["used"] = max(rec["used"], used or 0)
                    rec["limit"] = max(rec["limit"], total_gb)
                    if expiry and (not rec["expiry"]
                                   or expiry > rec["expiry"]):
                        rec["expiry"] = expiry
                    rec["enable"] = rec["enable"] and enable
                    if v["id"] not in rec["inbounds"]:
                        rec["inbounds"].append(v["id"])
                    rec["status"] = client_status(rec["enable"],
                                                  rec["expiry"],
                                                  rec["used"], rec["limit"])
        return [merged[u] for u in order]

    def list_merged_clients(self):
        """v6.1.5: one record per customer (email) across Outline +
        VLESS — the list the dashboard, server page, search and export
        all render. A dual-protocol customer appears ONCE (protocol
        "both"); Outline-only / VLESS-only customers appear as before
        (protocol "outline" / "vless")."""
        return merge_xui_clients(self.list_clients(),
                                 self.list_vless_clients())

    def get_merged_client(self, email):
        """Merged record by email, or None."""
        for c in self.list_merged_clients():
            if c["name"] == email:
                return c
        return None

    def _merged_sides(self, email):
        """(has_outline, has_vless) for one email — which underlying
        records a dual action must touch."""
        cur = self.get_merged_client(email)
        if not cur:
            raise PanelError("client not found: %s" % email)
        return cur, bool(cur.get("outline")), bool(cur.get("vless"))

    def _apply_both(self, email, outline_fn, vless_fn, what):
        """v6.1.5: run a dual action on both sides that exist. Each
        side is attempted; a side that fails but whose record is GONE
        anyway (e.g. the panel's delete already removed both) counts
        as done. Anything still present after a failure raises, naming
        the side — the row then visibly splits (old name on the failed
        side), never silently half-done."""
        cur, has_ol, has_vl = self._merged_sides(email)
        errors = []
        if has_ol:
            try:
                outline_fn()
            except Exception as exc:  # noqa: BLE001
                if self.get_client(email) is not None:
                    errors.append("outline: %s" % exc)
        if has_vl:
            try:
                vless_fn()
            except Exception as exc:  # noqa: BLE001
                if self.get_vless_client(email) is not None:
                    errors.append("vless: %s" % exc)
        if errors:
            raise PanelError("%s partially failed for %s — %s"
                             % (what, email, "; ".join(errors)))
        got = self.get_merged_client(email)
        return got

    def renew_merged_client(self, email, months):
        """v6.1.5: renew BOTH sides (same new name + expiry, computed
        once so the two records can never diverge). GB unchanged.
        Returns (merged_record, new_name)."""
        cur, has_ol, has_vl = self._merged_sides(email)
        _base, old_date = parse_name_date(email)
        if old_date is None:
            if not cur.get("expiry"):
                raise PanelError("renew: no expiry date on %s" % email)
            old_date = (datetime.fromtimestamp(cur["expiry"], MM)
                        - timedelta(days=1)).date()
        new_name, new_date = renew_name_and_date(email, old_date, months)
        new_exp_ms = int((datetime(new_date.year, new_date.month,
                                   new_date.day, tzinfo=MM)
                          + timedelta(days=1)).timestamp() * 1000)
        if has_ol:
            self.update_client(email, email=new_name,
                               expiryTime=new_exp_ms)
        if has_vl:
            self.update_vless_client(email, email=new_name,
                                     expiryTime=new_exp_ms)
        got = self.get_merged_client(new_name)
        if not got:
            raise PanelError("renew: client not found on panel after "
                             "update")
        return got, new_name

    def swap_merged_client(self, email):
        """v6.1.5: swap BOTH sides — fresh Outline password + fresh
        VLESS UUID, same name / expiry / GB. Returns the merged
        record."""
        cur, has_ol, has_vl = self._merged_sides(email)
        errors = []
        if has_ol:
            try:
                self.swap_client(email)
            except Exception as exc:  # noqa: BLE001
                errors.append("outline: %s" % exc)
        if has_vl:
            try:
                self.swap_vless_client(email)
            except Exception as exc:  # noqa: BLE001
                errors.append("vless: %s" % exc)
        if errors:
            raise PanelError("swap partially failed for %s — %s"
                             % (email, "; ".join(errors)))
        got = self.get_merged_client(email)
        if not got:
            raise PanelError("swap: client not found on panel after swap")
        return got

    def toggle_merged_client(self, email, enable):
        """v6.1.5: enable/disable BOTH sides."""
        self._apply_both(
            email,
            lambda: self.update_client(email, enable=bool(enable)),
            lambda: self.update_vless_client(email, enable=bool(enable)),
            "toggle")
        return self.get_merged_client(email)

    def set_gb_merged_client(self, email, total_gb_bytes):
        """v6.1.5: set the GB limit on BOTH sides."""
        self._apply_both(
            email,
            lambda: self.update_client(email,
                                       totalGB=int(total_gb_bytes)),
            lambda: self.update_vless_client(email,
                                             totalGB=int(total_gb_bytes)),
            "set GB")
        return self.get_merged_client(email)

    def edit_merged_client(self, email, gb_bytes=None, new_date=None):
        """v6.1.5: edit GB and/or expiry on BOTH sides (a date change
        RENAMES both records to the same new date). Returns
        (merged_record, new_name)."""
        cur, has_ol, has_vl = self._merged_sides(email)
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
        errors = []
        if has_ol:
            try:
                self.update_client(email, **changes)
            except Exception as exc:  # noqa: BLE001
                errors.append("outline: %s" % exc)
        if has_vl:
            try:
                self.update_vless_client(email, **changes)
            except Exception as exc:  # noqa: BLE001
                errors.append("vless: %s" % exc)
        if errors:
            raise PanelError("edit partially failed for %s — %s"
                             % (email, "; ".join(errors)))
        got = self.get_merged_client(new_name)
        if not got:
            raise PanelError("edit: client not found on panel after "
                             "update")
        return got, new_name

    def delete_merged_client(self, email):
        """v6.1.5: delete BOTH sides. The panel's /clients/del/{email}
        removes the email from every inbound, so the second delete may
        find its side already gone — that counts as done, not a
        failure. Verifies the merged record is gone."""
        cur, has_ol, has_vl = self._merged_sides(email)
        errors = []
        if has_ol:
            try:
                self.delete_client(email)
            except Exception as exc:  # noqa: BLE001
                if self.get_client(email) is not None:
                    errors.append("outline: %s" % exc)
        if has_vl:
            try:
                self.delete_vless_client(email)
            except Exception as exc:  # noqa: BLE001
                if self.get_vless_client(email) is not None:
                    errors.append("vless: %s" % exc)
        if errors:
            raise PanelError("delete partially failed for %s — %s"
                             % (email, "; ".join(errors)))
        if self.get_merged_client(email) is not None:
            raise PanelError("delete: client still on panel after delete")
        return True

    def get_vless_client(self, email):
        """Merged VLESS record by email, or None."""
        for c in self.list_vless_clients():
            if c["name"] == email:
                return c
        return None

    def _raw_vless_client(self, email, inbound_id):
        """The raw VLESS client dict from ONE inbound's settings."""
        r = self._request("GET", "/panel/api/inbounds/get/%s" % inbound_id)
        try:
            obj = r.json().get("obj", {})
        except Exception:
            return None
        settings = _as_dict(obj.get("settings"))
        for c in settings.get("clients", []) or []:
            if c.get("email") == email:
                return dict(c)
        return None

    def _vless_payload(self, name, new_uuid, total_gb_bytes, expiry_ms):
        """Client dict for a VLESS add — same UUID everywhere, security
        auto, limitIp 0 (the shape the studied panels carry)."""
        return {"email": name, "id": new_uuid, "totalGB": total_gb_bytes,
                "expiryTime": expiry_ms, "enable": True,
                "limitIp": 0, "security": "auto"}

    def create_vless_client(self, name, total_gb_bytes, expiry_ms):
        """Create one UUID on EVERY vless inbound (one add call with all
        ids, then verify per inbound and add individually to any that
        missed it). Returns the merged record."""
        vl = self.detect_inbounds()["vless"]
        if not vl:
            raise PanelError("no VLESS inbound on this server")
        new_uuid = str(uuid.uuid4())
        payload = {"client": self._vless_payload(name, new_uuid,
                                                 total_gb_bytes, expiry_ms),
                   "inboundIds": [v["id"] for v in vl]}
        r = self._request("POST", "/panel/api/clients/add",
                          json=payload,
                          headers={"Content-Type": "application/json"})
        try:
            res = r.json()
        except Exception:
            raise PanelError("create vless: bad response from panel")
        if not res.get("success"):
            raise PanelError("create vless failed: %s"
                             % res.get("msg", r.text[:200]))
        self._inbounds_cache = None
        for v in vl:
            if self._raw_vless_client(name, v["id"]) is None:
                r2 = self._request(
                    "POST", "/panel/api/clients/add",
                    json={"client": self._vless_payload(
                        name, new_uuid, total_gb_bytes, expiry_ms),
                        "inboundIds": [v["id"]]},
                    headers={"Content-Type": "application/json"})
                try:
                    res2 = r2.json()
                except Exception:
                    res2 = {}
                if not res2.get("success"):
                    raise PanelError("create vless: inbound %s failed: %s"
                                     % (v["id"], res2.get("msg", "")))
                self._inbounds_cache = None
        rec = self.get_vless_client(name)
        if not rec or rec.get("uuid") != new_uuid:
            raise PanelError("create vless: client not found on panel "
                             "after add")
        return rec

    def update_vless_client(self, old_email, **changes):
        """Update the client on EVERY vless inbound that has it. Same
        pattern as update_client: the FULL raw record is sent back, only
        the changed fields differ — plus the `id` (the UUID) as a STRING.
        The first parameter is `old_email` (not `email`) so a rename
        change (email=new_name) lands in **changes instead of colliding
        with it — the same convention as update_client."""
        cur = self.get_vless_client(old_email)
        if not cur:
            raise PanelError("update vless: client not found: %s" % old_email)
        for inb_id in cur["inbounds"]:
            raw = self._raw_vless_client(old_email, inb_id)
            if not raw:
                continue
            if raw.get("id"):
                raw["id"] = str(raw["id"])
            raw.update(changes)
            r = self._request(
                "POST", "/panel/api/clients/update/" +
                urllib.parse.quote(old_email),
                json=raw, headers={"Content-Type": "application/json"})
            try:
                res = r.json()
            except Exception:
                raise PanelError("update vless: bad response from panel")
            if not res.get("success"):
                raise PanelError("update vless failed: %s"
                                 % res.get("msg", r.text[:200]))
        self._inbounds_cache = None
        new_email = changes.get("email", old_email)
        got = verify_with_retry(lambda: self.get_vless_client(new_email))
        if not got:
            raise PanelError("update vless: client not found on panel "
                             "after update")
        return got

    def renew_vless_client(self, email, months):
        """Renew: new expiry = old expiry + 30/60/90 days; the client is
        RENAMED to the new date (same name convention as Outline) on
        every vless inbound. GB is unchanged. Returns
        (new_record, new_name)."""
        cur = self.get_vless_client(email)
        if not cur:
            raise PanelError("renew vless: client not found: %s" % email)
        _base, old_date = parse_name_date(email)
        if old_date is None:
            if not cur.get("expiry"):
                raise PanelError("renew vless: no expiry date on %s" % email)
            old_date = (datetime.fromtimestamp(cur["expiry"], MM)
                        - timedelta(days=1)).date()
        new_name, new_date = renew_name_and_date(email, old_date, months)
        new_exp_ms = int((datetime(new_date.year, new_date.month,
                                   new_date.day, tzinfo=MM)
                          + timedelta(days=1)).timestamp() * 1000)
        rec = self.update_vless_client(email, email=new_name,
                                       expiryTime=new_exp_ms)
        return rec, new_name

    def swap_vless_client(self, email):
        """Swap: DELETE the old client first, then create a fresh one
        with the SAME name / expiry / GB but a NEW UUID (the UUID is the
        VLESS credential — a new UUID is the fresh password)."""
        cur = self.get_vless_client(email)
        if not cur:
            raise PanelError("swap vless: client not found: %s" % email)
        was_enabled = bool(cur.get("enable", True))
        self.delete_vless_client(email)
        try:
            rec = self.create_vless_client(
                email, cur["limit"] or 0,
                int(cur["expiry"] * 1000) if cur.get("expiry") else 0)
        except Exception:
            raise PanelError("swap vless: old key deleted but recreate "
                             "failed — recreate %s manually on the panel"
                             % email)
        if not was_enabled:
            rec = self.update_vless_client(email, enable=False)
        return rec

    def toggle_vless_client(self, email, enable):
        return self.update_vless_client(email, enable=bool(enable))

    def set_gb_vless_client(self, email, total_gb_bytes):
        return self.update_vless_client(email, totalGB=int(total_gb_bytes))

    def edit_vless_client(self, email, gb_bytes=None, new_date=None):
        """Edit GB limit and/or expiry date (v3 pattern). A date change
        RENAMES the key to the new date on every vless inbound.
        Returns (new_record, new_name)."""
        cur = self.get_vless_client(email)
        if not cur:
            raise PanelError("edit vless: client not found: %s" % email)
        changes = {}
        new_name = email
        if new_date is not None:
            _base, old_date = parse_name_date(email)
            if old_date is None:
                if not cur.get("expiry"):
                    raise PanelError("edit vless: no expiry date on %s"
                                     % email)
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
        rec = self.update_vless_client(email, **changes)
        return rec, new_name

    def delete_vless_client(self, email):
        """Delete by email (the panel removes it from every inbound),
        then verify it is GONE from the merged VLESS list."""
        r = self._request("POST", "/panel/api/clients/del/" +
                          urllib.parse.quote(email))
        try:
            res = r.json()
            if not res.get("success"):
                raise PanelError("delete vless failed: %s"
                                 % res.get("msg", r.text[:200]))
        except PanelError:
            raise
        except Exception:
            pass  # some panels return non-JSON; verify below instead
        self._inbounds_cache = None
        if verify_with_retry(lambda: self.get_vless_client(email),
                             expect_present=False) is not None:
            raise PanelError("delete vless: client still on panel after "
                             "delete")
        return True

    def build_vless_links(self, client):
        """WS link (primary, port 443) + XHTTP link (secondary, port 2087)
        from the vless inbounds' streamSettings. HOST/SERVERNAME = the
        panel domain (per the study) — unless an inbound has a Custom
        share address (v6.1.7: detect_inbounds() then reports that
        address as host + server_name, so the links use it as @host,
        sni= and &host=). Returns {"ws", "xhttp", "primary"}.
        Other networks get a best-effort generic link under their network
        name."""
        vl = self.detect_inbounds()["vless"]
        if not vl:
            raise PanelError("no VLESS inbound on this server")
        uid = client.get("uuid") or ""
        if not uid:
            raise PanelError("no UUID on record for this VLESS client")
        name = urllib.parse.quote(client["name"], safe="")
        out = {"ws": None, "xhttp": None, "primary": None}
        for v in vl:
            net = (v.get("network") or "").lower()
            host = v.get("host") or v.get("server_name") or ""
            sni = v.get("server_name") or host
            path = urllib.parse.quote(v.get("path") or "", safe="/")
            port = v.get("port") or 443
            fp = v.get("fingerprint") or "chrome"
            sec = v.get("security") or "tls"
            if net == "ws":
                out["ws"] = (
                    "vless://%s@%s:%d?encryption=none&security=%s&sni=%s"
                    "&type=ws&host=%s&path=%s&fp=%s#%s"
                    % (uid, host, port, sec, sni, host, path, fp, name))
            elif net == "xhttp":
                mode = v.get("mode") or "auto"
                out["xhttp"] = (
                    "vless://%s@%s:%d?encryption=none&security=%s&sni=%s"
                    "&type=xhttp&mode=%s&host=%s&path=%s&fp=%s#%s"
                    % (uid, host, port, sec, sni, mode, host, path, fp,
                       name))
            else:
                out[net] = (
                    "vless://%s@%s:%d?encryption=none&security=%s&sni=%s"
                    "&type=%s&host=%s&fp=%s#%s"
                    % (uid, host, port, sec, sni, net, host, fp, name))
        out["primary"] = out.get("ws") or out.get("xhttp") or next(
            (l for k, l in out.items() if k != "primary" and l), None)
        return out

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
        """Create on the detected Outline inbound, then GET it back to
        read the password."""
        ol = self.detect_inbounds()["outline"]
        if ol is None:
            raise PanelError("no Outline/shadowsocks inbound on this server")
        payload = {"client": {"email": name, "totalGB": total_gb_bytes,
                              "expiryTime": expiry_ms, "enable": True},
                   "inboundIds": [ol["id"]]}
        r = self._request("POST", "/panel/api/clients/add",
                          json=payload,
                          headers={"Content-Type": "application/json"})
        try:
            res = r.json()
        except Exception:
            raise PanelError("create: bad response from panel")
        if not res.get("success"):
            raise PanelError("create failed: %s" % res.get("msg", r.text[:200]))
        self._inbounds_cache = None
        # verify: read the client back from the Outline inbound
        # (password is generated by the panel)
        r = self._request("GET", "/panel/api/inbounds/get/%s" % ol["id"])
        obj = r.json().get("obj", {})
        settings = _as_dict(obj.get("settings"))
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
        """The raw client dict straight from the Outline inbound settings
        (keeps id, flow, limitIp, tgId, subId, comment, reset — everything
        the update endpoint expects back), or None."""
        ol = self.detect_inbounds()["outline"]
        if ol is None:
            return None
        r = self._request("GET", "/panel/api/inbounds/get/%s" % ol["id"])
        try:
            obj = r.json().get("obj", {})
        except Exception:
            return None
        settings = _as_dict(obj.get("settings"))
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
        self._inbounds_cache = None
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
        self._inbounds_cache = None
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
        limit_total_bytes) — the limit total feeds the dashboard usage bar.
        v6.1.5: counts MERGED customers (one per email), not Outline +
        VLESS records — a dual-protocol customer used to count twice
        here too (and its used/limit twice in the bar totals)."""
        clients = self.list_merged_clients()
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
            # v6.1: online = seen within ONLINE_WINDOW_SECS. Marzban's
            # online_at is a naive ISO string; treated as UTC (to be
            # confirmed by the live connect test in TEST-REPORT.md).
            online = False
            oa = u.get("online_at")
            if oa:
                try:
                    dt = datetime.fromisoformat(str(oa).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    online = (time.time() - dt.timestamp()) <= ONLINE_WINDOW_SECS
                except (ValueError, TypeError):
                    online = False
            out.append({
                "name": u.get("username", ""),
                "enable": enable,
                "used": used,
                "limit": limit,
                "expiry": expire,
                "status": st,
                "method": meth,
                "password": pw,
                "online": online,
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

    def list_inbounds(self):
        """Raw inbounds (for the seed probe: REPORT any VLESS inbound
        found — Marzban VLESS is out of scope, never managed). Returns
        the parsed JSON (list or dict, depending on the Marzban version)
        or None."""
        r = self._request("GET", "/api/inbounds")
        try:
            return r.json()
        except Exception:
            return None

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
