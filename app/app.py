#!/usr/bin/env python3
"""
Spazcat IPAM - a lightweight IP allocation / device tracking tool.

- Pulls clients (active + configured reservations) from a UniFi controller.
- Organizes them into color-coded pools you define in the UI.
- New/unknown devices land in the gray "Unallocated" pool.
- Offline devices grey out; after a grace period unlocked ones fall back to
  Unallocated so the IP stays visible. Locked devices never move.
- Duplicate IPs are flagged. Manual devices are never auto-moved.
- Everything exportable to CSV so you can leave UniFi DHCP behind later.

Single-file Flask app. SQLite for storage. No build step on the frontend.
"""

import csv
import io
import ipaddress
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

import requests
import urllib3
import secrets
from datetime import timedelta
from flask import Flask, g, jsonify, request, Response, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_PATH = os.environ.get("IPAM_DB", "/data/ipam.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=None)

DEFAULT_POOL_COLOR = "#6b7280"  # gray

# ----------------------------------------------------------------------------
# Auth (optional, env-driven) - off by default; enable to expose over WAN
# ----------------------------------------------------------------------------
#   IPAM_AUTH_ENABLED        "true" to require login (default off)
#   IPAM_AUTH_USER           username (default "admin")
#   IPAM_AUTH_PASSWORD       plaintext password (hashed in memory at boot)
#   IPAM_AUTH_PASSWORD_HASH  pre-hashed password (werkzeug format); wins if set
#   IPAM_SECRET_KEY          session-signing secret (else persisted in DB)
#   IPAM_SESSION_DAYS        session lifetime in days (default 30)
#   IPAM_COOKIE_SECURE       "true" to mark the cookie Secure (behind HTTPS)
AUTH_ENABLED = os.environ.get("IPAM_AUTH_ENABLED", "").lower() in ("1", "true", "yes", "on")
AUTH_USER = os.environ.get("IPAM_AUTH_USER", "admin")
_pw_hash_env = os.environ.get("IPAM_AUTH_PASSWORD_HASH", "").strip()
_pw_plain = os.environ.get("IPAM_AUTH_PASSWORD", "")
SESSION_DAYS = int(os.environ.get("IPAM_SESSION_DAYS", "30") or 30)
COOKIE_SECURE = os.environ.get("IPAM_COOKIE_SECURE", "").lower() in ("1", "true", "yes", "on")

if _pw_hash_env:
    AUTH_HASH = _pw_hash_env
elif _pw_plain:
    AUTH_HASH = generate_password_hash(_pw_plain)
else:
    AUTH_HASH = None

if AUTH_ENABLED and not AUTH_HASH:
    print("[auth] WARNING: IPAM_AUTH_ENABLED is set but no password was provided "
          "(IPAM_AUTH_PASSWORD or IPAM_AUTH_PASSWORD_HASH). Auth is DISABLED.", flush=True)
    AUTH_ENABLED = False

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
)
# fallback secret so sessions never crash before configure_secret() runs
app.secret_key = secrets.token_hex(32)

_AUTH_PUBLIC = {"/login", "/api/login", "/api/logout", "/favicon.ico"}


def configure_secret():
    """Stable session secret: env override, else a value persisted in the DB so
    logins survive restarts. Called at startup after init_db()."""
    env = os.environ.get("IPAM_SECRET_KEY", "").strip()
    if env:
        app.secret_key = env
        return
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        val = get_setting(db, "secret_key", "")
        if not val:
            val = secrets.token_hex(32)
            set_setting(db, "secret_key", val)
            db.commit()
        app.secret_key = val


@app.before_request
def _require_auth():
    if not AUTH_ENABLED:
        return
    p = request.path
    if p in _AUTH_PUBLIC or p.startswith("/static/"):
        return
    if session.get("authed"):
        return
    if p.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect("/login")


# ----------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS pools (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                color      TEXT NOT NULL DEFAULT '#6b7280',
                subnets    TEXT NOT NULL DEFAULT '',
                notes      TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS devices (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                mac        TEXT UNIQUE,
                name       TEXT NOT NULL DEFAULT '',
                hostname   TEXT NOT NULL DEFAULT '',
                ip         TEXT NOT NULL DEFAULT '',
                pool_id    INTEGER NOT NULL,
                source     TEXT NOT NULL DEFAULT 'manual',
                is_online  INTEGER NOT NULL DEFAULT 1,
                is_reserved INTEGER NOT NULL DEFAULT 0,
                locked     INTEGER NOT NULL DEFAULT 0,
                notes      TEXT NOT NULL DEFAULT '',
                first_seen INTEGER,
                last_seen  INTEGER,
                FOREIGN KEY (pool_id) REFERENCES pools(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS excluded (
                mac         TEXT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                ip          TEXT NOT NULL DEFAULT '',
                excluded_at INTEGER
            );
            """
        )
        # Ensure a default pool exists
        row = db.execute("SELECT id FROM pools WHERE is_default = 1").fetchone()
        if not row:
            db.execute(
                "INSERT INTO pools (name, color, subnets, notes, sort_order, is_default) "
                "VALUES (?,?,?,?,?,1)",
                ("Unallocated", DEFAULT_POOL_COLOR, "", "New / unsorted devices land here", -1),
            )
        # Lightweight migrations for existing databases
        cols = {r[1] for r in db.execute("PRAGMA table_info(devices)").fetchall()}
        if "locked" not in cols:
            db.execute("ALTER TABLE devices ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        # default settings
        defaults = {
            "unifi_host": "",
            "unifi_api_key": "",
            "unifi_site": "default",
            "unifi_verify_ssl": "0",
            "grace_days": "7",
            "auto_sync_minutes": "0",
            "theme": "dark",
            "font_size": "18",
        }
        for k, v in defaults.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v)
            )
        db.commit()


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def default_pool_id(db):
    row = db.execute("SELECT id FROM pools WHERE is_default=1").fetchone()
    return row["id"] if row else None


def now_ts():
    return int(time.time())


# ----------------------------------------------------------------------------
# IP / subnet helpers
# ----------------------------------------------------------------------------

def validate_subnets(raw):
    """Parse a comma/whitespace separated list of CIDRs. Returns
    (normalized_str, error_or_None). Empty input is allowed (no error).
    Every token must be strict CIDR notation, e.g. 10.0.1.0/24."""
    raw = (raw or "").strip()
    if not raw:
        return "", None
    tokens = [t for t in raw.replace(",", " ").split() if t]
    norm = []
    for t in tokens:
        if "/" not in t:
            return None, f"'{t}' needs a CIDR prefix, e.g. {t}/24"
        try:
            net = ipaddress.ip_network(t, strict=False)
        except ValueError:
            return None, f"'{t}' is not valid CIDR notation"
        norm.append(str(net))
    return ", ".join(norm), None


def subnet_sort_key(subnets):
    """Sortable key from the first CIDR's network address. Pools with no valid
    subnet sort last."""
    raw = (subnets or "").strip()
    if raw:
        first = raw.replace(",", " ").split()[0]
        try:
            net = ipaddress.ip_network(first, strict=False)
            return (0, int(net.network_address))
        except ValueError:
            pass
    return (1, 0)


def ip_sort_key(ip):
    try:
        return (0, int(ipaddress.ip_address((ip or "").strip())))
    except ValueError:
        return (1, 0)


# ----------------------------------------------------------------------------
# UniFi client
# ----------------------------------------------------------------------------

class UniFiError(Exception):
    pass


class UniFiClient:
    """UniFi OS console (UDM / UCG / UDR / UX / UniFi OS Server) via a stateless
    API key. The key is generated inside the Network application under
    Settings -> Integrations and sent as the X-API-KEY header - no login,
    session, or CSRF handling. It inherits the creating admin's permissions and
    works against the proxied classic Network API, which still exposes connected
    clients (stat/sta) and known clients + fixed-IP reservations (rest/user)."""

    def __init__(self, host, api_key, site="default", verify_ssl=False):
        host = (host or "").strip().rstrip("/")
        if not host.startswith("http"):
            host = "https://" + host
        self.host = host
        self.api_key = (api_key or "").strip()
        self.site = site or "default"
        self.session = requests.Session()
        self.session.verify = verify_ssl

    # login() kept as a harmless no-op so existing call sites don't break
    def login(self):
        return

    def _api_base(self):
        return f"{self.host}/proxy/network/api/s/{self.site}"

    def _get(self, path):
        headers = {"X-API-KEY": self.api_key, "Accept": "application/json"}
        try:
            r = self.session.get(
                f"{self._api_base()}{path}", headers=headers, timeout=15
            )
        except requests.RequestException as e:
            raise UniFiError(f"Connection failed: {e}")
        if r.status_code in (401, 403):
            raise UniFiError(
                "Auth rejected - check the API key (Network app -> Settings -> "
                "Integrations) and that the host is reachable."
            )
        if r.status_code != 200:
            raise UniFiError(f"API {path} returned HTTP {r.status_code}")
        try:
            return r.json().get("data", [])
        except ValueError:
            raise UniFiError(f"Unexpected non-JSON response from {path}")

    def get_clients(self):
        """Returns merged dict keyed by mac with active + configured info."""
        active = self._get("/stat/sta")        # currently connected
        known = self._get("/rest/user")         # all known/configured (has fixed_ip)

        merged = {}
        for u in known:
            mac = (u.get("mac") or "").lower()
            if not mac:
                continue
            merged[mac] = {
                "mac": mac,
                "name": u.get("name") or "",
                "hostname": u.get("hostname") or "",
                "ip": u.get("fixed_ip") or u.get("last_ip") or "",
                "is_reserved": bool(u.get("use_fixedip")) or bool(u.get("fixed_ip")),
                "online": False,
                "last_seen": u.get("last_seen") or 0,
            }
        for c in active:
            mac = (c.get("mac") or "").lower()
            if not mac:
                continue
            entry = merged.setdefault(mac, {
                "mac": mac, "name": "", "hostname": "", "ip": "",
                "is_reserved": False, "online": False, "last_seen": 0,
            })
            entry["online"] = True
            entry["ip"] = c.get("ip") or entry["ip"]
            entry["hostname"] = c.get("hostname") or entry["hostname"]
            entry["name"] = c.get("name") or entry["name"]
            if c.get("use_fixedip") or c.get("fixed_ip"):
                entry["is_reserved"] = True
                entry["ip"] = c.get("fixed_ip") or entry["ip"]
            entry["last_seen"] = c.get("last_seen") or entry["last_seen"]
        return merged


def build_unifi_from_settings(db):
    host = get_setting(db, "unifi_host", "")
    api_key = get_setting(db, "unifi_api_key", "")
    site = get_setting(db, "unifi_site", "default")
    verify = (get_setting(db, "unifi_verify_ssl", "0") or "0") in ("1", "true", "True")
    if not host or not api_key:
        raise UniFiError("UniFi controller is not configured.")
    return UniFiClient(host, api_key, site, verify_ssl=verify)


# ----------------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------------

def serialize_state(db):
    pools = db.execute("SELECT * FROM pools").fetchall()
    devices = db.execute("SELECT * FROM devices").fetchall()

    # duplicate-IP detection (ignore blanks)
    ip_counts = {}
    for d in devices:
        ip = (d["ip"] or "").strip()
        if ip:
            ip_counts[ip] = ip_counts.get(ip, 0) + 1
    dup_ips = {ip for ip, n in ip_counts.items() if n > 1}

    dev_by_pool = {}
    for d in sorted(devices, key=lambda r: (ip_sort_key(r["ip"]), r["name"] or "")):
        dev_by_pool.setdefault(d["pool_id"], []).append({
            "id": d["id"],
            "mac": d["mac"],
            "name": d["name"],
            "hostname": d["hostname"],
            "ip": d["ip"],
            "pool_id": d["pool_id"],
            "source": d["source"],
            "is_online": bool(d["is_online"]),
            "is_reserved": bool(d["is_reserved"]),
            "locked": bool(d["locked"]),
            "dup_ip": (d["ip"] or "").strip() in dup_ips,
            "notes": d["notes"],
            "last_seen": d["last_seen"],
        })

    # order: default pool first, then by first subnet CIDR, then name
    ordered = sorted(
        pools,
        key=lambda p: (0 if p["is_default"] else 1,
                       subnet_sort_key(p["subnets"]),
                       (p["name"] or "").lower()),
    )
    out_pools = []
    for p in ordered:
        out_pools.append({
            "id": p["id"],
            "name": p["name"],
            "color": p["color"],
            "subnets": p["subnets"],
            "notes": p["notes"],
            "sort_order": p["sort_order"],
            "is_default": bool(p["is_default"]),
            "devices": dev_by_pool.get(p["id"], []),
        })

    # build a readable duplicate summary for the top-of-page banner
    dup_summary = []
    for ip in sorted(dup_ips, key=ip_sort_key):
        names = [
            (d["name"] or d["hostname"] or d["mac"] or "?")
            for d in devices if (d["ip"] or "").strip() == ip
        ]
        dup_summary.append({"ip": ip, "devices": names})

    settings = {
        "unifi_host": get_setting(db, "unifi_host", ""),
        "unifi_key_set": bool(get_setting(db, "unifi_api_key", "")),
        "unifi_site": get_setting(db, "unifi_site", "default"),
        "unifi_verify_ssl": (get_setting(db, "unifi_verify_ssl", "0") or "0") in ("1","true","True"),
        "grace_days": int(get_setting(db, "grace_days", "7") or 7),
        "auto_sync_minutes": int(get_setting(db, "auto_sync_minutes", "0") or 0),
        "last_sync": int(get_setting(db, "last_sync", "0") or 0),
        "theme": get_setting(db, "theme", "dark"),
        "font_size": int(get_setting(db, "font_size", "18") or 18),
        "auth_enabled": AUTH_ENABLED,
    }
    excluded = [dict(r) for r in db.execute(
        "SELECT mac, name, ip, excluded_at FROM excluded ORDER BY excluded_at DESC"
    ).fetchall()]
    return {
        "pools": out_pools,
        "settings": settings,
        "total": len(devices),
        "duplicates": dup_summary,
        "excluded": excluded,
    }


# ----------------------------------------------------------------------------
# Routes - frontend
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/login")
def login_page():
    if not AUTH_ENABLED or session.get("authed"):
        return redirect("/")
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    if not AUTH_ENABLED:
        return jsonify({"ok": True})  # nothing to do
    data = request.get_json(force=True) or {}
    user = (data.get("username") or "").strip()
    pw = data.get("password") or ""
    # constant-ish time: always run the hash check
    ok = bool(AUTH_HASH) and user == AUTH_USER and check_password_hash(AUTH_HASH, pw)
    if not ok:
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401
    session.clear()
    session["authed"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)


# ----------------------------------------------------------------------------
# Routes - state
# ----------------------------------------------------------------------------

@app.route("/api/state")
def api_state():
    db = get_db()
    return jsonify(serialize_state(db))


# ----------------------------------------------------------------------------
# Routes - pools
# ----------------------------------------------------------------------------

@app.route("/api/pools", methods=["POST"])
def create_pool():
    db = get_db()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip() or "New Pool"
    color = (data.get("color") or "#8b5cf6").strip()
    subnets, err = validate_subnets(data.get("subnets"))
    if err:
        return jsonify({"error": err}), 400
    notes = (data.get("notes") or "").strip()
    row = db.execute("SELECT MAX(sort_order) AS m FROM pools").fetchone()
    nxt = (row["m"] or 0) + 1
    cur = db.execute(
        "INSERT INTO pools (name,color,subnets,notes,sort_order,is_default) VALUES (?,?,?,?,?,0)",
        (name, color, subnets, notes, nxt),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid})


@app.route("/api/pools/<int:pid>", methods=["PUT"])
def update_pool(pid):
    db = get_db()
    data = request.get_json(force=True)
    fields = []
    values = []
    for key in ("name", "color", "subnets", "notes"):
        if key in data:
            val = (data[key] or "").strip()
            if key == "subnets":
                val, err = validate_subnets(val)
                if err:
                    return jsonify({"error": err}), 400
            fields.append(f"{key}=?")
            values.append(val)
    if fields:
        values.append(pid)
        db.execute(f"UPDATE pools SET {','.join(fields)} WHERE id=?", values)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/pools/<int:pid>", methods=["DELETE"])
def delete_pool(pid):
    db = get_db()
    pool = db.execute("SELECT * FROM pools WHERE id=?", (pid,)).fetchone()
    if not pool:
        return jsonify({"error": "not found"}), 404
    if pool["is_default"]:
        return jsonify({"error": "cannot delete default pool"}), 400
    # move devices back to default
    dflt = default_pool_id(db)
    db.execute("UPDATE devices SET pool_id=? WHERE pool_id=?", (dflt, pid))
    db.execute("DELETE FROM pools WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/pools/reorder", methods=["POST"])
def reorder_pools():
    db = get_db()
    data = request.get_json(force=True)
    order = data.get("order", [])
    for idx, pid in enumerate(order):
        db.execute(
            "UPDATE pools SET sort_order=? WHERE id=? AND is_default=0", (idx, pid)
        )
    db.commit()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# Routes - devices
# ----------------------------------------------------------------------------

@app.route("/api/devices", methods=["POST"])
def create_device():
    db = get_db()
    data = request.get_json(force=True)
    pool_id = data.get("pool_id") or default_pool_id(db)
    ts = now_ts()
    mac = (data.get("mac") or "").strip().lower() or None
    # manually adding a previously-excluded MAC clears its exclusion
    if mac:
        db.execute("DELETE FROM excluded WHERE mac=?", (mac,))
    cur = db.execute(
        "INSERT INTO devices (mac,name,hostname,ip,pool_id,source,is_online,is_reserved,locked,notes,first_seen,last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            mac,
            (data.get("name") or "").strip(),
            (data.get("hostname") or "").strip(),
            (data.get("ip") or "").strip(),
            pool_id,
            "manual",
            1,
            1 if data.get("is_reserved") else 0,
            1 if data.get("locked") else 0,
            (data.get("notes") or "").strip(),
            ts, ts,
        ),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid})


@app.route("/api/devices/<int:did>", methods=["PUT"])
def update_device(did):
    db = get_db()
    data = request.get_json(force=True)
    fields = []
    values = []
    for key in ("name", "hostname", "ip", "notes", "pool_id", "is_reserved", "locked"):
        if key in data:
            fields.append(f"{key}=?")
            val = data[key]
            if key in ("is_reserved", "locked"):
                val = 1 if val else 0
            elif isinstance(val, str):
                val = val.strip()
            values.append(val)
    if fields:
        values.append(did)
        db.execute(f"UPDATE devices SET {','.join(fields)} WHERE id=?", values)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/devices/<int:did>", methods=["DELETE"])
def delete_device(did):
    db = get_db()
    row = db.execute("SELECT mac, name, ip FROM devices WHERE id=?", (did,)).fetchone()
    # a device with a MAC is excluded on delete so UniFi's long connection
    # history can't resurrect it on the next sync (restorable in Settings)
    if row and row["mac"]:
        db.execute(
            "INSERT OR REPLACE INTO excluded (mac, name, ip, excluded_at) VALUES (?,?,?,?)",
            (row["mac"], row["name"] or "", row["ip"] or "", now_ts()),
        )
    db.execute("DELETE FROM devices WHERE id=?", (did,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/excluded", methods=["GET"])
def list_excluded():
    db = get_db()
    rows = db.execute(
        "SELECT mac, name, ip, excluded_at FROM excluded ORDER BY excluded_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/excluded/<mac>", methods=["DELETE"])
def restore_excluded(mac):
    db = get_db()
    db.execute("DELETE FROM excluded WHERE mac=?", (mac.strip().lower(),))
    db.commit()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# Routes - settings + unifi
# ----------------------------------------------------------------------------

@app.route("/api/settings", methods=["PUT"])
def update_settings():
    db = get_db()
    data = request.get_json(force=True)
    for key in ("unifi_host", "unifi_site", "grace_days", "auto_sync_minutes", "theme", "font_size"):
        if key in data:
            set_setting(db, key, data[key])
    if "unifi_verify_ssl" in data:
        set_setting(db, "unifi_verify_ssl", "1" if data["unifi_verify_ssl"] else "0")
    # only update the API key if a non-empty value is provided
    if data.get("unifi_api_key"):
        set_setting(db, "unifi_api_key", data["unifi_api_key"].strip())
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/unifi/test", methods=["POST"])
def unifi_test():
    db = get_db()
    data = request.get_json(force=True) or {}
    host = data.get("unifi_host") or get_setting(db, "unifi_host", "")
    # use the provided key, else fall back to the stored one
    api_key = data.get("unifi_api_key") or get_setting(db, "unifi_api_key", "")
    site = data.get("unifi_site") or get_setting(db, "unifi_site", "default")
    verify = bool(data.get("unifi_verify_ssl",
                  (get_setting(db, "unifi_verify_ssl", "0") or "0") in ("1","true","True")))
    if not host or not api_key:
        return jsonify({"ok": False, "error": "host and API key required"}), 400
    try:
        client = UniFiClient(host, api_key, site, verify_ssl=verify)
        clients = client.get_clients()
        return jsonify({"ok": True, "count": len(clients), "mode": "API key"})
    except UniFiError as e:
        return jsonify({"ok": False, "error": str(e)}), 200


def perform_sync(db):
    """Run one UniFi sync against the given db connection. Returns a result
    dict. Raises UniFiError if the controller can't be reached/authed."""
    client = build_unifi_from_settings(db)
    client.login()
    clients = client.get_clients()

    dflt = default_pool_id(db)
    ts = now_ts()
    seen_macs = set()
    added = 0
    updated = 0
    skipped = 0
    excluded = {r[0] for r in db.execute("SELECT mac FROM excluded").fetchall()}

    for mac, info in clients.items():
        if mac in excluded:
            skipped += 1
            continue
        seen_macs.add(mac)
        existing = db.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
        if existing:
            # keep user-set name/notes/pool; refresh network facts
            db.execute(
                "UPDATE devices SET ip=?, hostname=?, is_online=?, is_reserved=?, "
                "last_seen=?, name=CASE WHEN name='' THEN ? ELSE name END WHERE mac=?",
                (
                    info["ip"] or existing["ip"],
                    info["hostname"] or existing["hostname"],
                    1 if info["online"] else 0,
                    1 if info["is_reserved"] else existing["is_reserved"],
                    ts,
                    info["name"],
                    mac,
                ),
            )
            updated += 1
        else:
            db.execute(
                "INSERT INTO devices (mac,name,hostname,ip,pool_id,source,is_online,is_reserved,notes,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mac,
                    info["name"],
                    info["hostname"],
                    info["ip"],
                    dflt,
                    "unifi",
                    1 if info["online"] else 0,
                    1 if info["is_reserved"] else 0,
                    "",
                    ts, ts,
                ),
            )
            added += 1

    # Devices sourced from unifi that vanished from this fetch:
    #  - locked                -> left completely alone (never moves, never greys)
    #  - within grace window    -> grey out, stay in their pool
    #  - past grace window       -> grey out AND fall back to Unallocated so the
    #                              IP stays visible (never silently deleted)
    grace_days = int(get_setting(db, "grace_days", "7") or 7)
    grace_cutoff = ts - grace_days * 86400
    moved = 0
    greyed = 0
    rows = db.execute("SELECT * FROM devices WHERE source='unifi'").fetchall()
    for d in rows:
        if d["mac"] in seen_macs:
            continue
        if d["locked"]:
            continue  # protected reservation - leave as-is
        # not seen this sync -> it was removed from UniFi
        if (d["last_seen"] or 0) < grace_cutoff and d["pool_id"] != dflt:
            db.execute(
                "UPDATE devices SET is_online=0, pool_id=? WHERE id=?", (dflt, d["id"])
            )
            moved += 1
        else:
            db.execute("UPDATE devices SET is_online=0 WHERE id=?", (d["id"],))
            greyed += 1

    set_setting(db, "last_sync", str(ts))
    db.commit()
    return {
        "ok": True, "added": added, "updated": updated,
        "greyed": greyed, "moved": moved, "skipped": skipped, "mode": "API key",
    }


@app.route("/api/sync", methods=["POST"])
def sync():
    db = get_db()
    try:
        return jsonify(perform_sync(db))
    except UniFiError as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(STATIC_DIR, "favicon.svg", mimetype="image/svg+xml")


@app.route("/api/export.csv")
def export_csv():
    db = get_db()
    rows = db.execute(
        """
        SELECT d.ip, d.mac, d.name, d.hostname, p.name AS pool, p.subnets,
               d.is_reserved, d.locked, d.is_online, d.source, d.notes, d.last_seen
        FROM devices d JOIN pools p ON d.pool_id = p.id
        """
    ).fetchall()
    rows = sorted(rows, key=lambda r: (
        0 if r["pool"] == "Unallocated" else 1,
        subnet_sort_key(r["subnets"]),
        ip_sort_key(r["ip"]),
    ))

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ip", "mac", "name", "hostname", "pool", "pool_subnets",
                "reserved", "locked", "online", "source", "notes", "last_seen"])
    for r in rows:
        last = ""
        if r["last_seen"]:
            last = datetime.fromtimestamp(r["last_seen"], tz=timezone.utc).isoformat()
        w.writerow([
            r["ip"], r["mac"], r["name"], r["hostname"], r["pool"], r["subnets"],
            "yes" if r["is_reserved"] else "no",
            "yes" if r["locked"] else "no",
            "yes" if r["is_online"] else "no",
            r["source"], r["notes"], last,
        ])
    out = buf.getvalue()
    fname = f"ipam-export-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        out, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


def _auto_sync_loop():
    """Background timer: every 30s, check the auto_sync_minutes setting and run
    a sync if enough time has elapsed since the last one. Its own connection."""
    while True:
        time.sleep(30)
        try:
            with closing(sqlite3.connect(DB_PATH)) as db:
                db.row_factory = sqlite3.Row
                interval = int(get_setting(db, "auto_sync_minutes", "0") or 0)
                if interval <= 0:
                    continue
                last = int(get_setting(db, "last_sync", "0") or 0)
                if now_ts() - last < interval * 60:
                    continue
                try:
                    res = perform_sync(db)
                    print(f"[auto-sync] {res}", flush=True)
                except UniFiError as e:
                    print(f"[auto-sync] skipped: {e}", flush=True)
        except Exception as e:  # never let the loop die
            print(f"[auto-sync] error: {e}", flush=True)


def start_auto_sync():
    import threading
    t = threading.Thread(target=_auto_sync_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    init_db()
    configure_secret()
    start_auto_sync()
    port = int(os.environ.get("PORT", "20080"))
    app.run(host="0.0.0.0", port=port)
