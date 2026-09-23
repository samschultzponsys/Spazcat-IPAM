#!/usr/bin/env python3
"""
Spazcat IPAM - a lightweight IP allocation / device tracking tool.

- Pulls clients from a network controller / router (UniFi, TP-Link Omada,
  Alta Labs Route10, OpenWrt, MikroTik, OPNsense, Pi-hole - see platforms.py).
- Organizes them into color-coded pools you define in the UI; devices whose IP
  falls inside a pool's subnet can be sorted into it automatically.
- New/unknown devices with no matching pool land in the gray "Unallocated" pool.
- Offline devices grey out; after a grace period unlocked, non-reserved ones
  fall back to Unallocated so the IP stays visible. Locked devices never move.
- Duplicate IPs are flagged (only live claims count - stale history doesn't).
- Everything exportable to CSV so you can leave your DHCP server behind later.

Single-file Flask app (+ platforms.py). SQLite for storage. No frontend build.
"""

import csv
import io
import ipaddress
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import urllib3
from flask import Flask, g, jsonify, request, Response, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash

from platforms import (
    PlatformError, UniFiError, PLATFORMS, all_fields, make_platform, normalize_mac,
    platform_catalog, platform_fields, to_unix,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_PATH = os.environ.get("IPAM_DB", "/data/ipam.db")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
IMAGE_FONTS_DIR = os.path.join(STATIC_DIR, "fonts")      # baked into the image
DATA_FONTS_DIR = os.path.join(DATA_DIR, "fonts")          # drop-in, no rebuild
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
BACKUPS_KEPT = 10

app = Flask(__name__, static_folder=None)

DEFAULT_POOL_COLOR = "#6b7280"  # gray
DEFAULT_APP_NAME = "SPAZCAT IPAM"
SYNC_UNITS = {"minutes": 1, "hours": 60, "days": 1440}


# ----------------------------------------------------------------------------
# Version + changelog (CHANGELOG.md is the single source of truth)
# ----------------------------------------------------------------------------
#   Versions are always MAJOR.MINOR with a single-digit minor: 1.0 ... 1.9, 2.0.
#   The newest "## x.y" heading in CHANGELOG.md is the running version; the
#   GitHub Action reads the same heading to tag the image.

_VER_HEAD = re.compile(r"^##\s+\[?v?(\d+\.\d+)\]?(.*)$")


def load_changelog():
    for d in (APP_DIR, os.path.dirname(APP_DIR)):
        path = os.path.join(d, "CHANGELOG.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            break
    else:
        return []
    entries, cur = [], None
    for line in text.splitlines():
        m = _VER_HEAD.match(line.strip())
        if m:
            cur = {"version": m.group(1),
                   "date": m.group(2).strip(" -–—()[]"),
                   "body": []}
            entries.append(cur)
        elif cur is not None:
            cur["body"].append(line.rstrip())
    for e in entries:
        e["body"] = "\n".join(e["body"]).strip()
    return entries


CHANGELOG = load_changelog()
VERSION = CHANGELOG[0]["version"] if CHANGELOG else "0.0"


def version_key(v):
    try:
        major, minor = str(v).strip().lstrip("v").split(".")
        return (int(major), int(minor))
    except ValueError:
        return (-1, -1)


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
def _env_bool(name, default=""):
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


AUTH_ENABLED = _env_bool("IPAM_AUTH_ENABLED")
AUTH_USER = os.environ.get("IPAM_AUTH_USER", "admin")
_pw_hash_env = os.environ.get("IPAM_AUTH_PASSWORD_HASH", "").strip()
_pw_plain = os.environ.get("IPAM_AUTH_PASSWORD", "")
SESSION_DAYS = int(os.environ.get("IPAM_SESSION_DAYS", "30") or 30)
COOKIE_SECURE = _env_bool("IPAM_COOKIE_SECURE")

# Update check: which image to look at, and a hard off-switch
UPDATE_IMAGE = os.environ.get("IPAM_UPDATE_IMAGE", "ghcr.io/samschultzponsys/spazcat-ipam").strip()
UPDATE_CHECK_ALLOWED = os.environ.get("IPAM_UPDATE_CHECK", "true").lower() not in ("0", "false", "no", "off")
UPDATE_TTL = 6 * 3600

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

# the login page needs branding + fonts before anyone is signed in
_AUTH_PUBLIC = {"/login", "/api/login", "/api/logout", "/favicon.ico",
                "/api/branding", "/api/fonts.css"}


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
    if p in _AUTH_PUBLIC or p.startswith("/static/") or p.startswith("/fonts/"):
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


SETTING_DEFAULTS = {
    "platform": "unifi",
    "grace_days": "7",
    "auto_sync_minutes": "0",
    "auto_sync_value": "0",
    "auto_sync_unit": "minutes",
    "auto_pool_new": "1",          # new devices go straight into a matching pool
    "auto_pool_every_sync": "0",   # re-sort every unlocked device on every sync
    "prune_days": "0",             # forget stale offline clients after N days (0 = never)
    "theme": "dark",
    "font_size": "18",
    "app_name": DEFAULT_APP_NAME,
    "font_logo": "",
    "font_head": "",
    "font_body": "",
    "update_check": "1",
}


def backup_db(reason):
    """Consistent copy of the live DB into /data/backups (sqlite backup API, so
    it's safe even mid-write). Keeps the newest BACKUPS_KEPT files."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"ipam-{reason}-{stamp}.db")
    with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    olds = sorted((f for f in os.listdir(BACKUP_DIR) if f.startswith("ipam-") and f.endswith(".db")),
                  key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)))
    for f in olds[:-BACKUPS_KEPT]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass
    return dest


def init_db():
    """Create / migrate the schema. Migrations are additive only (new tables,
    new columns, new settings with INSERT OR IGNORE) so an existing /data/ipam.db
    always carries forward; before migrating to a new app version the DB is
    backed up to /data/backups."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    existed = os.path.isfile(DB_PATH) and os.path.getsize(DB_PATH) > 0
    if existed:
        with closing(sqlite3.connect(DB_PATH)) as db:
            has_settings = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
            prev = (db.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()
                    if has_settings else None)
        prev = prev[0] if prev else "pre-1.1"
        if prev != VERSION:
            try:
                path = backup_db(f"v{prev}")
                print(f"[db] upgrading {prev} -> {VERSION}; backup at {path}", flush=True)
            except Exception as e:  # a failed backup must not block startup
                print(f"[db] WARNING: pre-upgrade backup failed: {e}", flush=True)

    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
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

        # 1.1: auto-sync became value + unit; derive them from the old minutes
        have = {r[0] for r in db.execute("SELECT key FROM settings").fetchall()}
        if "auto_sync_value" not in have:
            mins = int((get_setting(db, "auto_sync_minutes", "0") or "0").strip() or 0)
            unit = "days" if mins and mins % 1440 == 0 else "hours" if mins and mins % 60 == 0 else "minutes"
            set_setting(db, "auto_sync_value", mins // SYNC_UNITS[unit])
            set_setting(db, "auto_sync_unit", unit)

        # 1.1: MACs are stored in one canonical form (aa:bb:cc:dd:ee:ff) so a
        # controller reporting AA-BB-.. can't create a second row for a device
        for table, key in (("devices", "id"), ("excluded", "mac")):
            for r in db.execute(f"SELECT {key} AS k, mac FROM {table} WHERE mac IS NOT NULL").fetchall():
                norm = normalize_mac(r["mac"])
                if norm and norm != r["mac"] and not db.execute(
                        f"SELECT 1 FROM {table} WHERE mac=?", (norm,)).fetchone():
                    db.execute(f"UPDATE {table} SET mac=? WHERE {key}=?", (norm, r["k"]))

        # default settings (never overwrite what's there)
        defaults = dict(SETTING_DEFAULTS)
        for f in all_fields().values():
            defaults.setdefault(f["key"], f["default"])
        for k, v in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))
        set_setting(db, "app_version", VERSION)
        db.commit()


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def setting_int(db, key, default=0):
    try:
        return int(float(get_setting(db, key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def setting_bool(db, key, default=False):
    v = get_setting(db, key, None)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def default_pool_id(db):
    row = db.execute("SELECT id FROM pools WHERE is_default=1").fetchone()
    return row[0] if row else None


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


def pool_networks(db):
    """[(network, pool_id, sort_order)] for every CIDR on every pool."""
    nets = []
    for p in db.execute("SELECT id, subnets, sort_order FROM pools").fetchall():
        for tok in (p["subnets"] or "").replace(",", " ").split():
            try:
                nets.append((ipaddress.ip_network(tok, strict=False), p["id"], p["sort_order"]))
            except ValueError:
                continue
    return nets


def match_pool(nets, ip):
    """Pool whose subnet contains ip. The most specific prefix wins, so a /28
    carved out of a /24 claims its devices; ties go to pool order."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    best = None
    for net, pid, so in nets:
        if addr.version == net.version and addr in net:
            key = (-net.prefixlen, so, pid)
            if best is None or key < best[0]:
                best = (key, pid)
    return best[1] if best else None


def is_stale(d, grace_cutoff):
    """Offline past the grace window with nothing pinning it: these belong in
    Unallocated, so auto-sort must not pull them back into a pool."""
    return (not d["is_online"] and not d["is_reserved"] and not d["locked"]
            and d["source"] != "manual" and (d["last_seen"] or 0) < grace_cutoff)


def sort_devices(db, only_pool=None):
    """Move every unlocked, non-stale device into the pool its IP matches.
    Devices with no matching pool stay where they are. Returns the count moved."""
    nets = pool_networks(db)
    if not nets:
        return 0
    cutoff = now_ts() - setting_int(db, "grace_days", 7) * 86400
    moved = 0
    for d in db.execute("SELECT * FROM devices WHERE locked=0").fetchall():
        if is_stale(d, cutoff):
            continue
        target = match_pool(nets, d["ip"])
        if target is None or target == d["pool_id"]:
            continue
        if only_pool is not None and target != only_pool:
            continue
        db.execute("UPDATE devices SET pool_id=? WHERE id=?", (target, d["id"]))
        moved += 1
    return moved


# ----------------------------------------------------------------------------
# Platform config
# ----------------------------------------------------------------------------

def platform_config(db, pid, overrides=None):
    """Stored settings for a platform's fields, with non-empty overrides (from
    an unsaved settings form) layered on top. Blank secrets keep the stored one."""
    cls = PLATFORMS.get(pid)
    if not cls:
        raise PlatformError(f"Unknown platform '{pid}'")
    cfg = {}
    for f in platform_fields(cls):
        cfg[f["key"]] = get_setting(db, f["key"], f["default"])
        if overrides and f["key"] in overrides:
            v = overrides[f["key"]]
            if f["kind"] == "checkbox":
                cfg[f["key"]] = "1" if v in (True, 1, "1", "true", "on") else "0"
            elif v not in (None, ""):
                cfg[f["key"]] = str(v)
    return cfg


def build_platform_from_settings(db):
    pid = get_setting(db, "platform", "unifi")
    return make_platform(pid, platform_config(db, pid))


# ----------------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------------
# Font files dropped into app/static/fonts/ (committed to GitHub -> baked into
# the image) or /data/fonts/ (runtime drop-in, no rebuild) become selectable
# for the logo, headers and body text in Settings -> Appearance.

FONT_FORMATS = {".otf": "opentype", ".ttf": "truetype", ".woff": "woff", ".woff2": "woff2"}
MONO_STACK = '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, monospace'
BUILTIN_FONTS = [
    {"id": "mono", "label": "Monospace", "css": MONO_STACK, "kind": "builtin"},
    {"id": "sans", "label": "System sans-serif", "kind": "builtin",
     "css": 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'},
    {"id": "serif", "label": "System serif", "kind": "builtin",
     "css": 'Georgia, Cambria, "Times New Roman", serif'},
]


def _font_slug(stem):
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "font"


def font_files():
    """Selectable font files. A /data/fonts file overrides a baked-in one with
    the same name."""
    found = {}
    for origin, folder, url_base in (("image", IMAGE_FONTS_DIR, "/static/fonts/"),
                                     ("data", DATA_FONTS_DIR, "/fonts/")):
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in FONT_FORMATS:
                continue
            slug = _font_slug(stem)
            label = re.sub(r"[_\-]+", " ", stem).strip()
            found[slug] = {
                "id": f"file:{slug}",
                "label": label[:1].upper() + label[1:],
                "family": f"ipamf-{slug}",
                "css": f'"ipamf-{slug}", {MONO_STACK}',
                "url": url_base + quote(fname),
                "format": FONT_FORMATS[ext.lower()],
                "origin": origin,
                "kind": "file",
            }
    return sorted(found.values(), key=lambda f: f["label"].lower())


def default_font_id(slot, files):
    if slot == "logo":
        # keep the original look: the Ethnocentric wordmark when it's present
        for f in files:
            s = f["id"]
            if "ethnocentric" in s and not re.search(r"(-it|italic)(-|$)", s):
                return s
    return "mono"


def resolve_fonts(db):
    files = font_files()
    options = BUILTIN_FONTS + files
    by_id = {o["id"]: o for o in options}
    resolved, defaults = {}, {}
    for slot in ("logo", "head", "body"):
        defaults[slot] = default_font_id(slot, files)
        chosen = get_setting(db, f"font_{slot}", "") or ""
        if chosen not in by_id:
            chosen = defaults[slot]
        resolved[slot] = by_id.get(chosen, BUILTIN_FONTS[0])["css"]
    return options, resolved, defaults


@app.route("/api/fonts.css")
def fonts_css():
    rules = []
    for f in font_files():
        rules.append(
            "@font-face {\n"
            f'  font-family: "{f["family"]}";\n'
            f'  src: url("{f["url"]}") format("{f["format"]}");\n'
            "  font-display: swap;\n}\n")
    return Response("".join(rules) or "/* no font files installed */\n",
                    mimetype="text/css", headers={"Cache-Control": "no-cache"})


@app.route("/fonts/<path:fname>")
def data_fonts(fname):
    return send_from_directory(DATA_FONTS_DIR, fname)


# ----------------------------------------------------------------------------
# Update check (queries the published image's tags on GHCR)
# ----------------------------------------------------------------------------

_update = {"latest": None, "checked_at": 0, "error": None}
_update_lock = threading.Lock()


def fetch_latest_version(image=UPDATE_IMAGE):
    """Newest x.y tag of the image in its registry (anonymous pull token -
    works for public GHCR packages)."""
    registry, _, repo = image.partition("/")
    tok = requests.get(f"https://{registry}/token",
                       params={"scope": f"repository:{repo}:pull"}, timeout=8)
    tok.raise_for_status()
    token = tok.json().get("token") or tok.json().get("access_token")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://{registry}/v2/{repo}/tags/list?n=1000"
    tags = []
    for _ in range(20):  # follow pagination, bounded
        r = requests.get(url, headers=headers, timeout=8)
        r.raise_for_status()
        tags += r.json().get("tags") or []
        link = r.headers.get("Link", "")
        m = re.search(r"<([^>]+)>", link)
        if not m:
            break
        nxt = m.group(1)
        url = nxt if nxt.startswith("http") else f"https://{registry}{nxt}"
    versions = [t.lstrip("v") for t in tags if re.fullmatch(r"v?\d+\.\d+", t)]
    return max(versions, key=version_key) if versions else None


def update_check_enabled(db):
    return UPDATE_CHECK_ALLOWED and setting_bool(db, "update_check", True)


def run_update_check():
    try:
        latest = fetch_latest_version()
        err = None
    except Exception as e:
        latest, err = None, str(e)
    with _update_lock:
        _update.update(latest=latest or _update["latest"], checked_at=now_ts(), error=err)
    if err:
        print(f"[update-check] {err}", flush=True)
    return _update


def update_status(db):
    enabled = update_check_enabled(db)
    latest = _update["latest"] if enabled else None
    return {
        "enabled": enabled,
        "allowed": UPDATE_CHECK_ALLOWED,
        "image": UPDATE_IMAGE,
        "latest": latest,
        "update_available": bool(latest and version_key(latest) > version_key(VERSION)),
        "checked_at": _update["checked_at"] if enabled else 0,
        "error": _update["error"] if enabled else None,
    }


# ----------------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------------

def _active_claim(d):
    """Does this row actually hold its IP right now? Online, reserved, manual
    and locked rows do; an offline dynamic client only *used* to."""
    return bool(d["is_online"] or d["is_reserved"] or d["locked"] or d["source"] == "manual")


def serialize_state(db):
    pools = db.execute("SELECT * FROM pools").fetchall()
    devices = db.execute("SELECT * FROM devices").fetchall()

    # Duplicate-IP detection. Only live claims count: controllers remember
    # every client they've ever seen with its *last* IP, so an old phone MAC and
    # today's laptop can both "have" 10.0.1.50 without any real conflict. Those
    # historic rows get stale_ip instead of tripping the duplicate banner.
    claims, newest_hist = {}, {}
    for d in devices:
        ip = (d["ip"] or "").strip()
        if not ip:
            continue
        if _active_claim(d):
            claims.setdefault(ip, []).append(d)
        else:
            newest_hist[ip] = max(newest_hist.get(ip, 0), d["last_seen"] or 0)
    dup_ips = {ip for ip, rows in claims.items() if len(rows) > 1}

    def stale_ip(d):
        ip = (d["ip"] or "").strip()
        if not ip or _active_claim(d):
            return False
        return ip in claims or (d["last_seen"] or 0) < newest_hist.get(ip, 0)

    dev_by_pool = {}
    stale_count = 0
    for d in sorted(devices, key=lambda r: (ip_sort_key(r["ip"]), r["name"] or "")):
        st = stale_ip(d)
        stale_count += st
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
            "stale_ip": st,
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
        names = [(d["name"] or d["hostname"] or d["mac"] or "?") for d in claims[ip]]
        dup_summary.append({"ip": ip, "devices": names})

    pid = get_setting(db, "platform", "unifi")
    fields = all_fields()
    platform_values, secrets_set = {}, {}
    for k, f in fields.items():
        if f["secret"]:
            secrets_set[k] = bool(get_setting(db, k, ""))   # never sent to the browser
        elif f["kind"] == "checkbox":
            platform_values[k] = setting_bool(db, k, f["default"] == "1")
        else:
            platform_values[k] = get_setting(db, k, f["default"]) or ""

    font_options, font_css, font_defaults = resolve_fonts(db)
    unit = get_setting(db, "auto_sync_unit", "minutes")
    settings = {
        "platform": pid,
        "platform_label": PLATFORMS[pid].label if pid in PLATFORMS else pid,
        "platforms": platform_catalog(),
        "platform_values": platform_values,
        "secrets_set": secrets_set,
        # legacy keys, kept for anything scripting against /api/state
        "unifi_host": get_setting(db, "unifi_host", ""),
        "unifi_key_set": bool(get_setting(db, "unifi_api_key", "")),
        "unifi_site": get_setting(db, "unifi_site", "default"),
        "unifi_verify_ssl": setting_bool(db, "unifi_verify_ssl"),
        "grace_days": setting_int(db, "grace_days", 7),
        "auto_sync_minutes": setting_int(db, "auto_sync_minutes", 0),
        "auto_sync_value": setting_int(db, "auto_sync_value", 0),
        "auto_sync_unit": unit if unit in SYNC_UNITS else "minutes",
        "auto_pool_new": setting_bool(db, "auto_pool_new", True),
        "auto_pool_every_sync": setting_bool(db, "auto_pool_every_sync", False),
        "prune_days": setting_int(db, "prune_days", 0),
        "last_sync": setting_int(db, "last_sync", 0),
        "theme": get_setting(db, "theme", "dark"),
        "font_size": setting_int(db, "font_size", 18),
        "app_name": get_setting(db, "app_name", DEFAULT_APP_NAME) or DEFAULT_APP_NAME,
        "font_logo": get_setting(db, "font_logo", ""),
        "font_head": get_setting(db, "font_head", ""),
        "font_body": get_setting(db, "font_body", ""),
        "font_options": font_options,
        "font_css": font_css,
        "font_defaults": font_defaults,
        "update_check": setting_bool(db, "update_check", True),
        "update_check_allowed": UPDATE_CHECK_ALLOWED,
        "auth_enabled": AUTH_ENABLED,
        "version": VERSION,
    }
    excluded = [dict(r) for r in db.execute(
        "SELECT mac, name, ip, excluded_at FROM excluded ORDER BY excluded_at DESC"
    ).fetchall()]
    return {
        "pools": out_pools,
        "settings": settings,
        "total": len(devices),
        "duplicates": dup_summary,
        "stale_count": stale_count,
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


@app.route("/api/branding")
def api_branding():
    db = get_db()
    _, css, _ = resolve_fonts(db)
    return jsonify({
        "app_name": get_setting(db, "app_name", DEFAULT_APP_NAME) or DEFAULT_APP_NAME,
        "font_logo": css["logo"],
        "theme": get_setting(db, "theme", "dark"),
    })


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
# Routes - state + version
# ----------------------------------------------------------------------------

@app.route("/api/state")
def api_state():
    db = get_db()
    return jsonify(serialize_state(db))


@app.route("/api/version")
def api_version():
    db = get_db()
    return jsonify({"version": VERSION, "changelog": CHANGELOG, "update": update_status(db)})


@app.route("/api/version/check", methods=["POST"])
def api_version_check():
    db = get_db()
    if update_check_enabled(db):
        run_update_check()
    return jsonify({"version": VERSION, "update": update_status(db)})


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
    moved = sort_devices(db, only_pool=cur.lastrowid) if data.get("claim_matching") else 0
    db.commit()
    return jsonify({"id": cur.lastrowid, "moved": moved})


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
    moved = sort_devices(db, only_pool=pid) if data.get("claim_matching") else 0
    db.commit()
    return jsonify({"ok": True, "moved": moved})


@app.route("/api/pools/auto-sort", methods=["POST"])
def auto_sort_pools():
    db = get_db()
    moved = sort_devices(db)
    db.commit()
    return jsonify({"ok": True, "moved": moved})


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
    ip = (data.get("ip") or "").strip()
    pool_id = data.get("pool_id")
    if not pool_id and setting_bool(db, "auto_pool_new", True):
        pool_id = match_pool(pool_networks(db), ip)
    pool_id = pool_id or default_pool_id(db)
    ts = now_ts()
    mac = normalize_mac(data.get("mac")) or None
    # manually adding a previously-excluded MAC clears its exclusion
    if mac:
        db.execute("DELETE FROM excluded WHERE mac=?", (mac,))
    try:
        cur = db.execute(
            "INSERT INTO devices (mac,name,hostname,ip,pool_id,source,is_online,is_reserved,locked,notes,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mac,
                (data.get("name") or "").strip(),
                (data.get("hostname") or "").strip(),
                ip,
                pool_id,
                "manual",
                1,
                1 if data.get("is_reserved") else 0,
                1 if data.get("locked") else 0,
                (data.get("notes") or "").strip(),
                ts, ts,
            ),
        )
    except sqlite3.IntegrityError:
        return jsonify({"error": f"A device with MAC {mac} already exists"}), 400
    db.commit()
    return jsonify({"id": cur.lastrowid})


@app.route("/api/devices/<int:did>", methods=["PUT"])
def update_device(did):
    db = get_db()
    data = request.get_json(force=True)
    fields = []
    values = []
    for key in ("name", "hostname", "ip", "notes", "pool_id", "is_reserved", "locked", "mac"):
        if key in data:
            fields.append(f"{key}=?")
            val = data[key]
            if key in ("is_reserved", "locked"):
                val = 1 if val else 0
            elif key == "mac":
                val = normalize_mac(val) or None
            elif isinstance(val, str):
                val = val.strip()
            values.append(val)
    if fields:
        values.append(did)
        try:
            db.execute(f"UPDATE devices SET {','.join(fields)} WHERE id=?", values)
        except sqlite3.IntegrityError:
            return jsonify({"error": "Another device already has that MAC"}), 400
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/devices/<int:did>", methods=["DELETE"])
def delete_device(did):
    db = get_db()
    row = db.execute("SELECT mac, name, ip FROM devices WHERE id=?", (did,)).fetchone()
    # a device with a MAC is excluded on delete so the controller's long
    # connection history can't resurrect it on the next sync (restorable in Settings)
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
    db.execute("DELETE FROM excluded WHERE mac=?", (normalize_mac(mac),))
    db.commit()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# Routes - settings + platforms
# ----------------------------------------------------------------------------

_PLAIN_SETTINGS = ("grace_days", "prune_days", "theme", "font_size", "app_name",
                   "font_logo", "font_head", "font_body")
_BOOL_SETTINGS = ("auto_pool_new", "auto_pool_every_sync", "update_check")


@app.route("/api/settings", methods=["PUT"])
def update_settings():
    db = get_db()
    data = request.get_json(force=True) or {}
    for key in _PLAIN_SETTINGS:
        if key in data:
            val = data[key]
            if key in ("grace_days", "prune_days", "font_size"):
                try:
                    val = max(0, int(float(val or 0)))
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} must be a number"}), 400
            elif key == "app_name":
                val = (str(val or "").strip() or DEFAULT_APP_NAME)[:60]
            set_setting(db, key, val)
    for key in _BOOL_SETTINGS:
        if key in data:
            set_setting(db, key, "1" if data[key] else "0")

    if "platform" in data:
        if data["platform"] not in PLATFORMS:
            return jsonify({"error": "unknown platform"}), 400
        set_setting(db, "platform", data["platform"])

    # auto-sync: value + unit (minutes / hours / days); the loop reads minutes
    if "auto_sync_value" in data or "auto_sync_unit" in data:
        unit = data.get("auto_sync_unit", get_setting(db, "auto_sync_unit", "minutes"))
        if unit not in SYNC_UNITS:
            return jsonify({"error": "auto_sync_unit must be minutes, hours or days"}), 400
        try:
            value = max(0, int(float(data.get("auto_sync_value",
                                              get_setting(db, "auto_sync_value", "0")) or 0)))
        except (TypeError, ValueError):
            return jsonify({"error": "auto-sync interval must be a number"}), 400
        set_setting(db, "auto_sync_value", value)
        set_setting(db, "auto_sync_unit", unit)
        set_setting(db, "auto_sync_minutes", value * SYNC_UNITS[unit])
    elif "auto_sync_minutes" in data:  # legacy callers
        mins = max(0, int(float(data["auto_sync_minutes"] or 0)))
        set_setting(db, "auto_sync_minutes", mins)
        set_setting(db, "auto_sync_value", mins)
        set_setting(db, "auto_sync_unit", "minutes")

    # platform connection fields; secrets only update when a value is given
    for key, f in all_fields().items():
        if key not in data:
            continue
        if f["kind"] == "checkbox":
            set_setting(db, key, "1" if data[key] else "0")
        elif f["secret"]:
            if data[key]:
                set_setting(db, key, str(data[key]).strip())
        else:
            set_setting(db, key, str(data[key] or "").strip())
    for key in data.get("clear_secrets") or []:
        f = all_fields().get(key)
        if f and f["secret"]:
            set_setting(db, key, "")
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/platform/test", methods=["POST"])
def platform_test():
    db = get_db()
    data = request.get_json(force=True) or {}
    pid = data.get("platform") or get_setting(db, "platform", "unifi")
    try:
        plat = make_platform(pid, platform_config(db, pid, data))
        clients = plat.get_clients()
    except PlatformError as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    online = sum(1 for c in clients.values() if c["online"])
    return jsonify({"ok": True, "count": len(clients), "online": online, "mode": plat.label})


@app.route("/api/unifi/test", methods=["POST"])
def unifi_test():
    """Legacy alias for /api/platform/test with UniFi."""
    data = dict(request.get_json(force=True) or {})
    data["platform"] = "unifi"
    db = get_db()
    try:
        plat = make_platform("unifi", platform_config(db, "unifi", data))
        return jsonify({"ok": True, "count": len(plat.get_clients()), "mode": plat.label})
    except PlatformError as e:
        return jsonify({"ok": False, "error": str(e)}), 200


def perform_sync(db):
    """Run one sync against the configured platform. Returns a result dict.
    Raises PlatformError if the controller can't be reached/authed."""
    pid = get_setting(db, "platform", "unifi")
    plat = build_platform_from_settings(db)
    clients = plat.get_clients()

    dflt = default_pool_id(db)
    ts = now_ts()
    grace_cutoff = ts - setting_int(db, "grace_days", 7) * 86400
    prune_days = setting_int(db, "prune_days", 0)
    prune_cutoff = ts - prune_days * 86400 if prune_days > 0 else None
    auto_new = setting_bool(db, "auto_pool_new", True)
    nets = pool_networks(db) if auto_new else []
    excluded = {normalize_mac(r[0]) for r in db.execute("SELECT mac FROM excluded").fetchall()}

    seen = {}          # mac -> is_reserved per the controller right now
    added = updated = skipped = ignored = pooled = 0

    for raw_mac, info in clients.items():
        mac = normalize_mac(raw_mac)
        if not mac:
            continue
        if mac in excluded:
            skipped += 1
            continue
        online = bool(info["online"])
        reserved = bool(info["is_reserved"])
        existing = db.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
        # online -> seen now. Offline -> the controller's own last-seen time,
        # NOT the sync time (stamping every remembered client with "now" is what
        # kept years-old history looking fresh and never let it age out).
        if online:
            last_seen = ts
        else:
            last_seen = to_unix(info.get("last_seen")) or (
                existing["last_seen"] if existing else 0) or ts

        # forget-after-N-days: don't import ancient history at all
        if (existing is None and prune_cutoff and not online and not reserved
                and last_seen < prune_cutoff):
            ignored += 1
            continue

        seen[mac] = reserved
        if existing:
            # keep user-set name/notes/pool; refresh network facts. For synced
            # rows the controller is the truth on reservations (a reservation
            # removed there must stop claiming its IP here).
            is_res = reserved if existing["source"] != "manual" else (reserved or bool(existing["is_reserved"]))
            db.execute(
                "UPDATE devices SET ip=?, hostname=?, is_online=?, is_reserved=?, "
                "last_seen=?, name=CASE WHEN name='' THEN ? ELSE name END WHERE mac=?",
                (
                    info["ip"] or existing["ip"],
                    info["hostname"] or existing["hostname"],
                    1 if online else 0,
                    1 if is_res else 0,
                    last_seen,
                    info["name"],
                    mac,
                ),
            )
            updated += 1
        else:
            pool = dflt
            fresh = online or reserved or last_seen >= grace_cutoff
            if auto_new and fresh:
                match = match_pool(nets, info["ip"])
                if match:
                    pool = match
                    pooled += 1
            db.execute(
                "INSERT INTO devices (mac,name,hostname,ip,pool_id,source,is_online,is_reserved,notes,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mac, info["name"], info["hostname"], info["ip"], pool, pid,
                 1 if online else 0, 1 if reserved else 0, "", ts, last_seen),
            )
            added += 1

    # Aging pass over every synced (non-manual) device:
    #  - locked                      -> left completely alone
    #  - gone from the controller    -> offline; its reservation went with it
    #  - offline past grace window   -> falls back to Unallocated so the IP stays
    #    (and not reserved)             visible (never silently deleted)
    #  - offline past prune window   -> forgotten (only if nothing user-entered
    #    (prune_days > 0)               hangs off it: no lock, notes, reservation)
    moved = pruned = 0
    for d in db.execute("SELECT * FROM devices WHERE source != 'manual'").fetchall():
        in_fetch = d["mac"] in seen
        online = bool(d["is_online"]) if in_fetch else False
        reserved = bool(d["is_reserved"]) if in_fetch else False
        if not in_fetch and (d["is_online"] or d["is_reserved"]):
            db.execute("UPDATE devices SET is_online=0, is_reserved=0 WHERE id=?", (d["id"],))
        if d["locked"] or online or reserved:
            continue
        last = d["last_seen"] or 0
        if prune_cutoff and last < prune_cutoff and not (d["notes"] or "").strip():
            db.execute("DELETE FROM devices WHERE id=?", (d["id"],))
            pruned += 1
        elif last < grace_cutoff and d["pool_id"] != dflt:
            db.execute("UPDATE devices SET pool_id=? WHERE id=?", (dflt, d["id"]))
            moved += 1

    sorted_n = sort_devices(db) if setting_bool(db, "auto_pool_every_sync", False) else 0
    offline = db.execute(
        "SELECT COUNT(*) FROM devices WHERE source != 'manual' AND is_online=0").fetchone()[0]

    set_setting(db, "last_sync", str(ts))
    db.commit()
    return {
        "ok": True, "added": added, "updated": updated, "greyed": offline,
        "moved": moved, "skipped": skipped, "ignored": ignored, "pruned": pruned,
        "pooled": pooled + sorted_n, "mode": plat.label,
    }


@app.route("/api/sync", methods=["POST"])
def sync():
    db = get_db()
    try:
        return jsonify(perform_sync(db))
    except PlatformError as e:
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
               p.is_default, d.is_reserved, d.locked, d.is_online, d.source,
               d.notes, d.last_seen
        FROM devices d JOIN pools p ON d.pool_id = p.id
        """
    ).fetchall()
    rows = sorted(rows, key=lambda r: (
        0 if r["is_default"] else 1,
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
    """Background timer: every 30s, check the auto-sync interval and run a sync
    if enough time has elapsed since the last one; refresh the update check
    every UPDATE_TTL. Its own connection."""
    while True:
        time.sleep(30)
        try:
            with closing(sqlite3.connect(DB_PATH)) as db:
                db.row_factory = sqlite3.Row
                if update_check_enabled(db) and now_ts() - _update["checked_at"] >= UPDATE_TTL:
                    run_update_check()
                interval = setting_int(db, "auto_sync_minutes", 0)
                if interval <= 0 or get_setting(db, "platform", "unifi") == "none":
                    continue
                last = max(setting_int(db, "last_sync", 0), setting_int(db, "last_sync_attempt", 0))
                if now_ts() - last < interval * 60:
                    continue
                try:
                    res = perform_sync(db)
                    print(f"[auto-sync] {res}", flush=True)
                except PlatformError as e:
                    # stamp the attempt so an unreachable controller isn't hammered every 30s
                    set_setting(db, "last_sync_attempt", str(now_ts()))
                    db.commit()
                    print(f"[auto-sync] skipped: {e}", flush=True)
        except Exception as e:  # never let the loop die
            print(f"[auto-sync] error: {e}", flush=True)


def start_auto_sync():
    t = threading.Thread(target=_auto_sync_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    print(f"[spazcat-ipam] v{VERSION}", flush=True)
    init_db()
    configure_secret()
    start_auto_sync()
    port = int(os.environ.get("PORT", "20080"))
    app.run(host="0.0.0.0", port=port)
