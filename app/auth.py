"""
Authentication for Spazcat IPAM.

Configured in the UI (Settings -> Security) and stored in the DB. Environment
variables override individual fields - useful for infrastructure-as-code and
for recovering from a broken UI config. `IPAM_AUTH_RESET=true` wipes the UI
config back to the defaults on startup.

Methods (per zone; any one enabled method is enough):
    none    no sign-in (cannot be combined with anything else)
    local   username + password (form, or HTTP Basic for scripts)
    oidc    OpenID Connect (Authentik, Authelia, Keycloak, Pocket ID, Google...)
    token   a secret in the URL (?token=...) or an Authorization: Bearer header

Zones: every request is LAN (client IP inside the LAN networks) or WAN. Behind
a reverse proxy the client IP comes from X-Forwarded-For, but only when the
direct peer is a trusted proxy - see client_ip().

Default (fresh install): `local` on LAN and WAN, user `admin` with a random
password printed to the container log on every start until it's changed.

Only a session signed in with `local` or `oidc` may change these settings, and
a save that would lock the editor out is refused (or needs confirming).

Environment overrides (each one locks its field in the UI):
    IPAM_AUTH_LAN / IPAM_AUTH_WAN           methods, comma-separated
    IPAM_AUTH_ENABLED=true                  legacy: `local` on both zones
    IPAM_LAN_NETWORKS  IPAM_TRUSTED_PROXIES  IPAM_PUBLIC_URL
    IPAM_AUTH_USER + IPAM_AUTH_PASSWORD | IPAM_AUTH_PASSWORD_HASH   (an env-managed user)
    IPAM_AUTH_USERS="alice:<hash-or-password>,..."                  (env-managed users)
    IPAM_AUTH_TOKENS="label:<secret>,..."   IPAM_AUTH_TOKEN_PARAM
    IPAM_OIDC_ISSUER  IPAM_OIDC_DISCOVERY_URL  IPAM_OIDC_CLIENT_ID  IPAM_OIDC_CLIENT_SECRET
    IPAM_OIDC_SCOPES  IPAM_OIDC_REDIRECT_URI  IPAM_OIDC_AUTO_LOGIN  IPAM_OIDC_BUTTON_TEXT
    IPAM_OIDC_ALLOWED_USERS  IPAM_OIDC_ALLOWED_GROUPS  IPAM_OIDC_GROUPS_CLAIM
    IPAM_SESSION_DAYS  IPAM_COOKIE_SECURE
    IPAM_AUTH_RESET=true                    reset the UI config (remove after use)
"""

import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests
from flask import g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

METHODS = ("none", "local", "oidc", "token")
EDIT_METHODS = ("local", "oidc")   # sessions allowed to change security settings
PRIVATE = "127.0.0.0/8, ::1/128, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7"
DEFAULT_LAN = PRIVATE + ", 169.254.0.0/16, fe80::/10"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
MIN_PASSWORD = 8

DEFAULT_OIDC = {
    "issuer": "", "discovery_url": "", "client_id": "", "client_secret": "",
    "scopes": "openid profile email", "redirect_uri": "", "auto_login": False,
    "button_text": "Sign in with SSO", "allowed_users": "", "allowed_groups": "",
    "groups_claim": "groups",
}
DEFAULT_STORED = {
    "lan": ["local"], "wan": ["local"],
    "lan_networks": DEFAULT_LAN, "trusted_proxies": PRIVATE, "public_url": "",
    "users": {}, "tokens": [], "token_param": "token",
    "oidc": DEFAULT_OIDC, "session_days": 30, "cookie_secure": False,
}

# paths reachable without a session in every zone (login page and what it needs)
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/api/auth/config", "/favicon.ico",
                "/api/branding", "/api/fonts.css", "/healthz",
                "/auth/oidc/login", "/auth/oidc/callback"}
PUBLIC_PREFIXES = ("/static/", "/fonts/")

_DB_PATH = None
_APP = None
CFG = None
_oauth = None
_oauth_sig = None
_lock = threading.RLock()
_fail_lock = threading.Lock()
_failures = {}   # ip -> [timestamps]
FAIL_LIMIT, FAIL_WINDOW = 10, 15 * 60


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _env_set(name):
    return bool(_env(name))


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _log(msg):
    print(f"[auth] {msg}", flush=True)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _ip(value):
    """Parse an address (tolerating [v6]:port / v4:port); unwrap v4-mapped v6."""
    v = (value or "").strip()
    if v.startswith("[") and "]" in v:
        v = v[1:v.index("]")]
    elif v.count(":") == 1:
        v = v.split(":")[0]
    try:
        a = ipaddress.ip_address(v)
    except ValueError:
        return None
    if a.version == 6 and a.ipv4_mapped:
        return a.ipv4_mapped
    return a


def parse_nets(raw, errors=None, what="network"):
    out = []
    for tok in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            if errors is not None:
                errors.append(f"'{tok}' is not a valid {what} (use CIDR, e.g. 192.168.1.0/24)")
            else:
                _log(f"WARNING: ignoring invalid {what} '{tok}'")
    return out


def _in(addr, nets):
    return addr is not None and any(addr.version == n.version and addr in n for n in nets)


def _methods(raw):
    if isinstance(raw, str):
        raw = raw.split(",")
    return [m.strip().lower() for m in raw or [] if str(m).strip()]


def new_password():
    """Readable random password: 4 groups of 4 from an unambiguous alphabet
    (~80 bits)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRTUVWXYZ2345679"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


# ----------------------------------------------------------------------------
# Stored config (DB) + effective config (DB overlaid with env)
# ----------------------------------------------------------------------------

def _db():
    db = sqlite3.connect(_DB_PATH, timeout=15)
    return db


def _get(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _put(db, key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def load_stored():
    with closing(_db()) as db:
        raw = _get(db, "auth_config")
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except ValueError:
        _log("ERROR: stored auth config is corrupt - using defaults. "
             "Set IPAM_AUTH_RESET=true to rewrite it.")
        return None
    merged = copy.deepcopy(DEFAULT_STORED)
    merged.update({k: v for k, v in stored.items() if k in DEFAULT_STORED})
    merged["oidc"] = {**DEFAULT_OIDC, **(stored.get("oidc") or {})}
    return merged


def save_stored(stored):
    with closing(_db()) as db:
        _put(db, "auth_config", json.dumps(stored))
        db.commit()


def initial_password():
    with closing(_db()) as db:
        return _get(db, "auth_initial_password") or ""


def clear_initial_password():
    with closing(_db()) as db:
        _put(db, "auth_initial_password", "")
        db.commit()


class AuthConfig:
    """Effective config: the stored (UI) config with env overrides applied.
    `locked` names every field the environment controls."""

    def __init__(self, stored, quiet=False):
        self.quiet = quiet
        self.errors = []
        self.locked = set()
        s = copy.deepcopy(stored or DEFAULT_STORED)
        o = {**DEFAULT_OIDC, **(s.get("oidc") or {})}

        def ov(env_name, field, current, cast=lambda v: v):
            if _env_set(env_name):
                self.locked.add(field)
                return cast(_env(env_name))
            return current

        self.lan_networks_raw = ov("IPAM_LAN_NETWORKS", "lan_networks", s["lan_networks"])
        self.trusted_raw = ov("IPAM_TRUSTED_PROXIES", "trusted_proxies", s["trusted_proxies"])
        self.lan_nets = parse_nets(self.lan_networks_raw)
        self.trusted = parse_nets(self.trusted_raw)
        self.public_url = ov("IPAM_PUBLIC_URL", "public_url", s["public_url"]).rstrip("/")
        self.session_days = ov("IPAM_SESSION_DAYS", "session_days", s["session_days"], int)
        self.cookie_secure = ov("IPAM_COOKIE_SECURE", "cookie_secure", s["cookie_secure"], _truthy)

        # --- local users: UI users, then env-managed users on top -----------
        self.users = {n: h for n, h in (s.get("users") or {}).items() if h}
        self.env_users = set()
        env_user = _env("IPAM_AUTH_USER", "admin") or "admin"
        pw_hash, pw = _env("IPAM_AUTH_PASSWORD_HASH"), os.environ.get("IPAM_AUTH_PASSWORD", "")
        if pw_hash or pw:
            self.users[env_user] = pw_hash or generate_password_hash(pw)
            self.env_users.add(env_user)
        for item in _env("IPAM_AUTH_USERS").split(","):
            if ":" not in item:
                continue
            name, secret = (x.strip() for x in item.split(":", 1))
            if name and secret:
                # werkzeug hashes look like "scrypt:32768:8:1$salt$hash"
                self.users[name] = secret if "$" in secret else generate_password_hash(secret)
                self.env_users.add(name)

        # --- tokens: stored as sha256; env tokens hashed on load -----------
        self.token_param = ov("IPAM_AUTH_TOKEN_PARAM", "token_param", s.get("token_param") or "token")
        self.tokens = [{"label": t["label"], "hash": t["hash"], "created": t.get("created"),
                        "env": False} for t in s.get("tokens") or [] if t.get("hash")]
        for i, item in enumerate(t for t in _env("IPAM_AUTH_TOKENS").split(",") if t.strip()):
            label, _, secret = item.strip().rpartition(":")
            secret = secret.strip()
            if len(secret) < 16 and not quiet:
                _log(f"WARNING: env token #{i + 1} is shorter than 16 characters")
            self.tokens.append({"label": label.strip() or f"env-token{i + 1}",
                                "hash": _sha(secret), "created": None, "env": True})

        # --- OIDC ------------------------------------------------------------
        def oov(env_name, key, cast=lambda v: v):
            return ov(env_name, f"oidc.{key}", o[key], cast)
        self.oidc_issuer = oov("IPAM_OIDC_ISSUER", "issuer").rstrip("/")
        disc = oov("IPAM_OIDC_DISCOVERY_URL", "discovery_url")
        self.oidc_discovery = disc or (
            f"{self.oidc_issuer}/.well-known/openid-configuration" if self.oidc_issuer else "")
        self.oidc_client_id = oov("IPAM_OIDC_CLIENT_ID", "client_id")
        self.oidc_client_secret = oov("IPAM_OIDC_CLIENT_SECRET", "client_secret")
        self.oidc_scopes = oov("IPAM_OIDC_SCOPES", "scopes") or "openid profile email"
        self.oidc_redirect_uri = oov("IPAM_OIDC_REDIRECT_URI", "redirect_uri")
        self.oidc_auto = oov("IPAM_OIDC_AUTO_LOGIN", "auto_login", _truthy)
        self.oidc_button = oov("IPAM_OIDC_BUTTON_TEXT", "button_text") or "Sign in with SSO"
        self.oidc_allowed_users = {u.strip().lower() for u in
                                   oov("IPAM_OIDC_ALLOWED_USERS", "allowed_users").split(",") if u.strip()}
        self.oidc_allowed_groups = {x.strip() for x in
                                    oov("IPAM_OIDC_ALLOWED_GROUPS", "allowed_groups").split(",") if x.strip()}
        self.oidc_groups_claim = oov("IPAM_OIDC_GROUPS_CLAIM", "groups_claim") or "groups"

        # --- zones -----------------------------------------------------------
        legacy = _truthy(_env("IPAM_AUTH_ENABLED"))
        self.zones = {}
        for z in ("lan", "wan"):
            if _env_set(f"IPAM_AUTH_{z.upper()}"):
                self.locked.add(z)
                raw = _methods(_env(f"IPAM_AUTH_{z.upper()}"))
            elif legacy:
                self.locked.add(z)
                raw = ["local"]
            else:
                raw = _methods(s.get(z))
            self.zones[z] = self._parse_zone(z, raw)

    def usable(self, m):
        if m == "local":
            return bool(self.users)
        if m == "token":
            return bool(self.tokens)
        if m == "oidc":
            return bool(self.oidc_discovery and self.oidc_client_id)
        return m == "none"

    def _parse_zone(self, zone, want):
        want = want or ["none"]
        bad = [m for m in want if m not in METHODS]
        if bad:
            self._err(f"{zone.upper()}: unknown sign-in method(s) {', '.join(bad)}")
        want = [m for m in dict.fromkeys(want) if m in METHODS]
        if "none" in want and len(want) > 1:
            self._err(f"{zone.upper()}: 'none' can't be combined with other methods - ignoring 'none'")
            want = [m for m in want if m != "none"]
        need = {"local": "at least one user", "token": "at least one access token",
                "oidc": "an OIDC issuer and client ID"}
        ok = []
        for m in want:
            if self.usable(m):
                ok.append(m)
            else:
                self._err(f"{zone.upper()}: '{m}' is enabled but not set up (needs {need[m]})")
        if want and not ok:
            # fail closed: never fall back to open because of a typo
            self._err(f"{zone.upper()}: no usable sign-in method - {zone.upper()} access is BLOCKED")
        return ok

    def _err(self, msg):
        self.errors.append(msg)
        if not self.quiet:
            _log("ERROR: " + msg)

    def describe(self):
        for z in ("lan", "wan"):
            src = " (from environment)" if z in self.locked else ""
            _log(f"{z.upper()}: {', '.join(self.zones[z]) or 'BLOCKED (misconfigured)'}{src}")


def _banner(password):
    lines = [
        "SPAZCAT IPAM  -  ADMIN SIGN-IN",
        "",
        "  username :  admin",
        f"  password :  {password}",
        "",
        "Change it in Settings -> Security.",
        "This box is printed on every start until you do.",
    ]
    width = max(len(x) for x in lines) + 6
    out = ["", "  ╔" + "═" * width + "╗"]
    for i, text in enumerate(lines):
        out.append("  ║" + text.center(width) + "║" if i == 0 else "  ║   " + text.ljust(width - 3) + "║")
        if i == 0:
            out.append("  ╠" + "═" * width + "╣")
    out += ["  ╚" + "═" * width + "╝", ""]
    print("\n".join(out), flush=True)


def bootstrap(db_path):
    """Called once the DB exists: apply IPAM_AUTH_RESET, create the default
    config (admin + generated password) on first run, then load it."""
    global _DB_PATH
    _DB_PATH = db_path
    if _truthy(_env("IPAM_AUTH_RESET")):
        with closing(_db()) as db:
            db.execute("DELETE FROM settings WHERE key IN ('auth_config','auth_initial_password')")
            db.commit()
        _log("IPAM_AUTH_RESET: security settings reset to defaults with a new admin "
             "password. REMOVE IPAM_AUTH_RESET from your compose file now.")
    stored = load_stored()
    if stored is None:
        stored = copy.deepcopy(DEFAULT_STORED)
        pw = new_password()
        stored["users"] = {"admin": generate_password_hash(pw)}
        save_stored(stored)
        with closing(_db()) as db:
            _put(db, "auth_initial_password", pw)
            db.commit()
    reload(verbose=True)
    pw = initial_password()
    admin_hash = (stored.get("users") or {}).get("admin")
    if pw and "admin" not in CFG.env_users and admin_hash and check_password_hash(admin_hash, pw):
        _banner(pw)
    elif pw:
        clear_initial_password()


def reload(verbose=False):
    """Rebuild the effective config from the DB and apply it to the app."""
    global CFG, _oauth_sig
    with _lock:
        cfg = AuthConfig(load_stored() or DEFAULT_STORED, quiet=not verbose)
        CFG = cfg
        if verbose:
            cfg.describe()
        if _APP is not None:
            _APP.config.update(SESSION_COOKIE_SECURE=cfg.cookie_secure,
                               PERMANENT_SESSION_LIFETIME=timedelta(days=max(1, cfg.session_days)))
            sig = (cfg.oidc_discovery, cfg.oidc_client_id, cfg.oidc_client_secret, cfg.oidc_scopes)
            if cfg.usable("oidc") and sig != _oauth_sig:
                _register_oidc(cfg)
                _oauth_sig = sig


def _register_oidc(cfg):
    global _oauth
    from authlib.integrations.flask_client import OAuth
    if _oauth is None:
        _oauth = OAuth(_APP)
    _oauth.register(
        "idp", overwrite=True,
        server_metadata_url=cfg.oidc_discovery,
        client_id=cfg.oidc_client_id,
        client_secret=cfg.oidc_client_secret or None,
        client_kwargs={"scope": cfg.oidc_scopes, "code_challenge_method": "S256"},
    )


def cfg():
    if CFG is None:
        reload()
    return CFG


# ----------------------------------------------------------------------------
# Client IP / zone
# ----------------------------------------------------------------------------

def peer_ip():
    return _ip(request.remote_addr)


def client_ip():
    """Real client address. X-Forwarded-For is only honored when the TCP peer
    is a trusted proxy, and it's read right-to-left so the first address that
    isn't itself a trusted proxy wins - a client can prepend anything it likes
    to the header but can't get past the hop the proxy appended."""
    c = cfg()
    peer = peer_ip()
    if not _in(peer, c.trusted):
        return peer
    hops = [h for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    addr = peer
    for h in reversed(hops):
        a = _ip(h)
        if a is None:
            break
        addr = a
        if not _in(a, c.trusted):
            return a
    if not hops:
        real = _ip(request.headers.get("X-Real-IP", ""))
        if real is not None:
            return real
    return addr


def via_proxy():
    return _in(peer_ip(), cfg().trusted) and bool(
        request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"))


def client_zone(c=None):
    return "lan" if _in(client_ip(), (c or cfg()).lan_nets) else "wan"


def zone_methods(zone=None):
    return cfg().zones[zone or client_zone()]


def external_base():
    """Base URL as the browser sees it (for the OIDC redirect URI / token links)."""
    c = cfg()
    if c.public_url:
        return c.public_url
    if via_proxy():
        proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
        host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
        return f"{proto}://{host}"
    return request.host_url.rstrip("/")


def redirect_uri():
    return cfg().oidc_redirect_uri or f"{external_base()}/auth/oidc/callback"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _safe_next(nxt):
    """Only same-site relative paths - no open redirects via ?next=."""
    nxt = nxt or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return "/"
    return nxt


def _rate_limited(ip):
    now = time.time()
    with _fail_lock:
        hits = [t for t in _failures.get(str(ip), []) if now - t < FAIL_WINDOW]
        _failures[str(ip)] = hits
        return len(hits) >= FAIL_LIMIT


def _record_failure(ip):
    now = time.time()
    with _fail_lock:
        if len(_failures) > 10000:  # bound memory under a many-IP flood
            for k in [k for k, v in _failures.items() if not v or now - v[-1] > FAIL_WINDOW]:
                del _failures[k]
        _failures.setdefault(str(ip), []).append(now)


def _match_token(value):
    if not value:
        return None
    digest = _sha(value)
    found = None
    for t in cfg().tokens:
        # compare against every token so timing doesn't reveal which one nearly matched
        if hmac.compare_digest(digest, t["hash"]):
            found = t["label"]
    return found


def _check_local(user, pw):
    users = cfg().users
    stored = users.get(user)
    # always run one hash check so unknown users take as long as known ones
    probe = stored or next(iter(users.values()), generate_password_hash("x"))
    return check_password_hash(probe, pw) and stored is not None


def _sign_in(method, user):
    session.clear()
    session.permanent = True
    session["authed"] = True
    session["method"] = method
    session["user"] = user


def _elevation_methods():
    """In a `none` zone you can still sign in (to edit security settings)."""
    c = cfg()
    return [m for m in ("local", "oidc") if c.usable(m)]


def login_methods(zone=None):
    methods = zone_methods(zone)
    return _elevation_methods() if methods == ["none"] else methods


def current():
    """(authed, method, user) for this request."""
    if getattr(g, "auth_via", None):
        return True, g.auth_via, g.auth_user
    if session.get("authed"):
        method, user = session.get("method"), session.get("user")
        if method == "local" and user not in cfg().users:
            return False, None, None      # user deleted since sign-in
        return True, method, user
    return False, None, None


def can_edit():
    authed, method, _ = current()
    return authed and method in EDIT_METHODS


def initial_password_active():
    authed, method, user = current()
    return bool(authed and method == "local" and user == "admin" and initial_password())


def request_status():
    zone = client_zone()
    methods = zone_methods(zone)
    authed, method, user = current()
    return {
        "zone": zone,
        "methods": methods,
        "required": methods != ["none"],
        "authed_via": method if authed else None,
        "user": user if authed else None,
        "can_logout": authed,
        "can_edit": can_edit(),
        "initial_password": initial_password_active(),
    }


def security_report():
    """Everything Settings -> Security shows. Never includes a secret."""
    c = cfg()
    peer = peer_ip()
    warnings = list(c.errors)
    ip = client_ip()
    if peer and not via_proxy() and _in(peer, [ipaddress.ip_network("172.16.0.0/12")]) \
            and str(peer).endswith(".1"):
        warnings.append(f"Your connection arrives from {peer}, which looks like a Docker "
                        "gateway. If every client shows this address, Docker is hiding real "
                        "client IPs and WAN traffic could be treated as LAN. Put a reverse "
                        "proxy in front, or publish the port without the userland proxy.")
    if "none" in c.zones["wan"]:
        warnings.append("WAN has no sign-in. Don't forward this app to the internet like this.")
    stored = load_stored() or DEFAULT_STORED
    so = stored["oidc"]
    return {
        **request_status(),
        "client_ip": str(ip) if ip else None,
        "peer_ip": str(peer) if peer else None,
        "via_proxy": via_proxy(),
        "locked": sorted(c.locked),
        "effective": {
            "lan": c.zones["lan"], "wan": c.zones["wan"],
            "lan_networks": [str(n) for n in c.lan_nets],
            "trusted_proxies": [str(n) for n in c.trusted],
        },
        # the editable (stored) values, with env-overridden fields showing the env value
        "config": {
            "lan": c.zones["lan"] if "lan" in c.locked else stored["lan"],
            "wan": c.zones["wan"] if "wan" in c.locked else stored["wan"],
            "lan_networks": c.lan_networks_raw,
            "trusted_proxies": c.trusted_raw,
            "public_url": c.public_url,
            "session_days": c.session_days,
            "cookie_secure": c.cookie_secure,
            "token_param": c.token_param,
            "oidc": {
                "issuer": c.oidc_issuer if "oidc.issuer" in c.locked else so["issuer"],
                "discovery_url": so["discovery_url"] if "oidc.discovery_url" not in c.locked
                                 else _env("IPAM_OIDC_DISCOVERY_URL"),
                "client_id": c.oidc_client_id,
                "client_secret_set": bool(c.oidc_client_secret),
                "scopes": c.oidc_scopes,
                "redirect_uri": c.oidc_redirect_uri,
                "auto_login": c.oidc_auto,
                "button_text": c.oidc_button,
                "allowed_users": ", ".join(sorted(c.oidc_allowed_users)),
                "allowed_groups": ", ".join(sorted(c.oidc_allowed_groups)),
                "groups_claim": c.oidc_groups_claim,
            },
        },
        "users": [{"name": n, "env": n in c.env_users} for n in sorted(c.users)],
        "tokens": [{"label": t["label"], "env": t["env"], "created": t["created"]} for t in c.tokens],
        "oidc_redirect_uri": redirect_uri(),
        "oidc_ready": c.usable("oidc"),
        "public_url_effective": external_base(),
        "warnings": warnings,
    }


def _strip_token_url():
    parts = urlsplit(request.full_path if request.query_string else request.path)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k != cfg().token_param]
    return urlunsplit(("", "", parts.path, urlencode(q), ""))


def _deny(msg, code=403):
    return jsonify({"ok": False, "error": msg}), code


# ----------------------------------------------------------------------------
# Config changes (Settings -> Security)
# ----------------------------------------------------------------------------

def _would_lock_out(candidate):
    """Would the current editor lose access after this save? Returns a
    message, or None if they'd still be signed in."""
    c = AuthConfig(candidate, quiet=True)
    zone = client_zone(c)
    methods = c.zones[zone]
    if methods == ["none"]:
        return None
    if not methods:
        return f"This would block {zone.upper()} access - the network you're on."
    authed, method, user = current()
    if not authed or method not in methods:
        return (f"You're signed in with '{method}', which {zone.upper()} (your network) "
                f"would no longer accept. You'd have to sign in with {' or '.join(methods)}.")
    if method == "local" and user not in c.users:
        return "Your own user would no longer exist."
    return None


def _validate(candidate):
    errors = []
    for z in ("lan", "wan"):
        want = _methods(candidate[z])
        if not want:
            errors.append(f"{z.upper()}: pick at least one option (or 'No sign-in')")
        if "none" in want and len(want) > 1:
            errors.append(f"{z.upper()}: 'No sign-in' can't be combined with other methods")
        if any(m not in METHODS for m in want):
            errors.append(f"{z.upper()}: unknown method")
    parse_nets(candidate["lan_networks"], errors, "LAN network")
    parse_nets(candidate["trusted_proxies"], errors, "trusted proxy")
    pu = candidate["public_url"]
    if pu and not re.match(r"^https?://[^/\s]+", pu):
        errors.append("Public URL must look like https://ipam.example.com")
    c = AuthConfig(candidate, quiet=True)
    for z in ("lan", "wan"):
        if z in c.locked:
            continue
        for m in _methods(candidate[z]):
            if m in METHODS and m != "none" and not c.usable(m):
                need = {"local": "add a user first", "token": "create an access token first",
                        "oidc": "fill in the OIDC issuer and client ID first"}[m]
                errors.append(f"{z.upper()}: can't enable '{m}' - {need}")
    return errors


def _save_checked(candidate, confirm):
    errors = _validate(candidate)
    if errors:
        return jsonify({"ok": False, "error": " • ".join(dict.fromkeys(errors))}), 400
    lock = _would_lock_out(candidate)
    if lock and not confirm:
        return jsonify({"ok": False, "confirm": lock}), 409
    save_stored(candidate)
    reload()
    return jsonify({"ok": True})


def _editor_required():
    if not can_edit():
        return _deny("Sign in with a password or SSO to change security settings", 403)
    return None


# ----------------------------------------------------------------------------
# Flask wiring
# ----------------------------------------------------------------------------

def init_app(app):
    global _APP
    _APP = app

    @app.before_request
    def _gate():
        c = cfg()
        zone = client_zone(c)
        methods = c.zones[zone]
        path = request.path
        hdr = request.headers.get("Authorization", "")

        # scripts: HTTP Basic with a local user (never challenged, so browsers
        # don't pop a native dialog)
        if hdr.lower().startswith("basic ") and "local" in login_methods(zone):
            ip = client_ip()
            if _rate_limited(ip):
                return jsonify({"error": "too many attempts, try later"}), 429
            try:
                user, _, pw = base64.b64decode(hdr[6:].strip()).decode().partition(":")
            except Exception:
                user, pw = "", ""
            if _check_local(user, pw):
                g.auth_via, g.auth_user = "local", user
                return
            _record_failure(ip)

        # token auth: header for API/scripts, ?token= in the URL for browsers
        if "token" in methods:
            bearer = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
            bearer = bearer or request.headers.get("X-IPAM-Token", "").strip()
            label = _match_token(bearer)
            if label:
                g.auth_via, g.auth_user = "token", label
                return
            url_tok = request.args.get(c.token_param)
            if url_tok:
                ip = client_ip()
                if _rate_limited(ip):
                    return jsonify({"error": "too many attempts, try later"}), 429
                label = _match_token(url_tok)
                if label:
                    _sign_in("token", label)
                    if request.method == "GET" and not path.startswith("/api/"):
                        return redirect(_strip_token_url())   # drop the secret from the URL
                    return
                _record_failure(ip)

        if methods == ["none"]:
            return
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return
        authed, method, _ = current()
        # a session only counts if its method is allowed in the zone you're in
        # now (a LAN password login doesn't carry over to a WAN that wants SSO)
        if authed and method in methods:
            return
        if path.startswith("/api/"):
            return jsonify({"error": "authentication required", "zone": zone}), 401
        return redirect("/login?" + urlencode({"next": _safe_next(request.full_path.rstrip("?"))}))

    @app.route("/api/auth/config")
    def auth_config():
        c = cfg()
        zone = client_zone(c)
        methods = login_methods(zone)
        return jsonify({
            "zone": zone,
            "methods": c.zones[zone],
            "local": "local" in methods,
            "oidc": "oidc" in methods,
            "token": "token" in c.zones[zone],
            "oidc_button": c.oidc_button,
            "blocked": not c.zones[zone],
            "open": c.zones[zone] == ["none"],
        })

    @app.route("/api/login", methods=["POST"])
    def api_login():
        if "local" not in login_methods():
            return _deny("Password sign-in isn't enabled here")
        ip = client_ip()
        if _rate_limited(ip):
            return _deny("Too many failed attempts - try again in 15 minutes", 429)
        data = request.get_json(force=True, silent=True) or {}
        user = (data.get("username") or "").strip()
        if not _check_local(user, data.get("password") or ""):
            _record_failure(ip)
            return _deny("Invalid username or password", 401)
        _sign_in("local", user)
        return jsonify({"ok": True, "next": _safe_next(data.get("next"))})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True, "redirect": "/login?logged_out=1"})

    @app.route("/auth/oidc/login")
    def oidc_login():
        if "oidc" not in login_methods() or _oauth is None:
            return redirect("/login?error=" + "SSO sign-in isn't enabled here")
        session["oidc_next"] = _safe_next(request.args.get("next"))
        try:
            return _oauth.idp.authorize_redirect(redirect_uri())
        except Exception as e:
            _log(f"OIDC login failed: {e}")
            return redirect("/login?" + urlencode({"error": "Couldn't reach the identity provider",
                                                   "manual": "1"}))

    @app.route("/auth/oidc/callback")
    def oidc_callback():
        c = cfg()
        if "oidc" not in login_methods() or _oauth is None:
            return redirect("/login?error=" + "SSO sign-in isn't enabled here")
        fail = lambda msg: redirect("/login?" + urlencode({"error": msg, "manual": "1"}))
        if request.args.get("error"):
            return fail(request.args.get("error_description") or request.args["error"])
        try:
            token = _oauth.idp.authorize_access_token()
        except Exception as e:
            _log(f"OIDC callback failed: {e}")
            return fail("SSO sign-in failed - check the server log")
        claims = dict(token.get("userinfo") or {})
        if c.oidc_allowed_groups and c.oidc_groups_claim not in claims:
            try:  # some IdPs only put groups on the userinfo endpoint
                claims.update(_oauth.idp.userinfo(token=token))
            except Exception:
                pass
        names = {str(claims.get(k, "")).lower() for k in ("email", "preferred_username", "sub")} - {""}
        groups = claims.get(c.oidc_groups_claim) or []
        groups = {groups} if isinstance(groups, str) else set(map(str, groups))
        if c.oidc_allowed_users or c.oidc_allowed_groups:
            if not (names & c.oidc_allowed_users) and not (groups & c.oidc_allowed_groups):
                _log(f"OIDC user {sorted(names)} not in allowed users/groups")
                return fail("Your account isn't allowed to use this app")
        nxt = session.get("oidc_next", "/")
        user = claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "sso"
        _sign_in("oidc", user)
        return redirect(_safe_next(nxt))

    # --- Settings -> Security ------------------------------------------------

    @app.route("/api/auth/status")
    def api_auth_status():
        return jsonify(security_report())

    @app.route("/api/auth/settings", methods=["PUT"])
    def api_auth_settings():
        deny = _editor_required()
        if deny:
            return deny
        data = request.get_json(force=True, silent=True) or {}
        c = cfg()
        cand = load_stored() or copy.deepcopy(DEFAULT_STORED)
        for z in ("lan", "wan"):
            if z in data and z not in c.locked:
                cand[z] = _methods(data[z])
        for k in ("lan_networks", "trusted_proxies", "public_url", "token_param"):
            if k in data and k not in c.locked:
                cand[k] = str(data[k] or "").strip()
        cand["token_param"] = cand["token_param"] or "token"
        if "session_days" in data and "session_days" not in c.locked:
            try:
                cand["session_days"] = max(1, min(3650, int(data["session_days"])))
            except (TypeError, ValueError):
                return _deny("Session length must be a number of days", 400)
        if "cookie_secure" in data and "cookie_secure" not in c.locked:
            cand["cookie_secure"] = bool(data["cookie_secure"])
        o = data.get("oidc") or {}
        for k in DEFAULT_OIDC:
            if k in o and f"oidc.{k}" not in c.locked:
                if k == "client_secret":
                    if o[k]:
                        cand["oidc"][k] = str(o[k]).strip()
                elif k == "auto_login":
                    cand["oidc"][k] = bool(o[k])
                else:
                    cand["oidc"][k] = str(o[k] or "").strip()
        if o.get("clear_client_secret") and "oidc.client_secret" not in c.locked:
            cand["oidc"]["client_secret"] = ""
        return _save_checked(cand, bool(data.get("confirm")))

    @app.route("/api/auth/users", methods=["POST"])
    def api_add_user():
        deny = _editor_required()
        if deny:
            return deny
        data = request.get_json(force=True, silent=True) or {}
        name, pw = (data.get("username") or "").strip(), data.get("password") or ""
        if not USERNAME_RE.match(name):
            return _deny("Username: 1-64 letters, digits, . _ @ -", 400)
        if name in cfg().users:
            return _deny("That user already exists", 400)
        if len(pw) < MIN_PASSWORD:
            return _deny(f"Password must be at least {MIN_PASSWORD} characters", 400)
        cand = load_stored() or copy.deepcopy(DEFAULT_STORED)
        cand["users"][name] = generate_password_hash(pw)
        save_stored(cand)
        reload()
        return jsonify({"ok": True})

    @app.route("/api/auth/users/<name>", methods=["PUT", "DELETE"])
    def api_user(name):
        deny = _editor_required()
        if deny:
            return deny
        c = cfg()
        if name in c.env_users:
            return _deny("This user is set by environment variables - change it there", 400)
        cand = load_stored() or copy.deepcopy(DEFAULT_STORED)
        if name not in cand["users"]:
            return _deny("No such user", 404)
        if request.method == "PUT":
            pw = (request.get_json(force=True, silent=True) or {}).get("password") or ""
            if len(pw) < MIN_PASSWORD:
                return _deny(f"Password must be at least {MIN_PASSWORD} characters", 400)
            cand["users"][name] = generate_password_hash(pw)
            save_stored(cand)
            if name == "admin":
                clear_initial_password()
            reload()
            return jsonify({"ok": True})
        _, _, me = current()
        if name == me and session.get("method") == "local":
            return _deny("You can't delete the user you're signed in as", 400)
        del cand["users"][name]
        errors = [e for e in _validate(cand) if "'local'" in e]
        if errors:
            return _deny("Can't delete the last user while password sign-in is enabled. "
                         "Turn password sign-in off first.", 400)
        save_stored(cand)
        if name == "admin":
            clear_initial_password()
        reload()
        return jsonify({"ok": True})

    @app.route("/api/auth/tokens", methods=["POST"])
    def api_add_token():
        deny = _editor_required()
        if deny:
            return deny
        label = ((request.get_json(force=True, silent=True) or {}).get("label") or "").strip()
        if not USERNAME_RE.match(label):
            return _deny("Label: 1-64 letters, digits, . _ @ -", 400)
        if any(t["label"] == label for t in cfg().tokens):
            return _deny("A token with that label already exists", 400)
        secret = secrets.token_urlsafe(24)
        cand = load_stored() or copy.deepcopy(DEFAULT_STORED)
        cand["tokens"].append({"label": label, "hash": _sha(secret), "created": int(time.time())})
        save_stored(cand)
        reload()
        link = f"{external_base()}/?{urlencode({cfg().token_param: secret})}"
        return jsonify({"ok": True, "token": secret, "link": link})

    @app.route("/api/auth/tokens/<label>", methods=["DELETE"])
    def api_del_token(label):
        deny = _editor_required()
        if deny:
            return deny
        cand = load_stored() or copy.deepcopy(DEFAULT_STORED)
        before = len(cand["tokens"])
        cand["tokens"] = [t for t in cand["tokens"] if t["label"] != label]
        if len(cand["tokens"]) == before:
            return _deny("No such token (tokens from environment variables can't be revoked here)", 404)
        errors = [e for e in _validate(cand) if "'token'" in e]
        if errors:
            return _deny("That's the last token while access-link sign-in is enabled. "
                         "Turn it off first.", 400)
        save_stored(cand)
        reload()
        return jsonify({"ok": True})

    @app.route("/api/auth/oidc/test", methods=["POST"])
    def api_oidc_test():
        deny = _editor_required()
        if deny:
            return deny
        o = request.get_json(force=True, silent=True) or {}
        issuer = (o.get("issuer") or "").strip().rstrip("/")
        url = (o.get("discovery_url") or "").strip() or (
            f"{issuer}/.well-known/openid-configuration" if issuer else "")
        if not url:
            return _deny("Enter the issuer URL first", 400)
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            meta = r.json()
        except Exception as e:
            return jsonify({"ok": False, "error": f"Couldn't read {url}: {e}"})
        missing = [k for k in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
                   if not meta.get(k)]
        if missing:
            return jsonify({"ok": False, "error": f"Discovery document is missing {', '.join(missing)}"})
        return jsonify({"ok": True, "issuer": meta["issuer"],
                        "pkce": "S256" in (meta.get("code_challenge_methods_supported") or ["S256"])})


def login_redirect():
    """Where GET /login should go instead of rendering, or None to render it."""
    c = cfg()
    zone = client_zone(c)
    methods = c.zones[zone]
    elevate = bool(request.args.get("elevate"))
    if methods == ["none"] and not (elevate and _elevation_methods()):
        return "/"
    authed, method, _ = current()
    if authed and (method in methods or (elevate and method in EDIT_METHODS)):
        return _safe_next(request.args.get("next"))
    manual = any(request.args.get(k) for k in ("manual", "error", "logged_out", "elevate"))
    if c.oidc_auto and "oidc" in methods and not manual:
        return "/auth/oidc/login?" + urlencode({"next": _safe_next(request.args.get("next"))})
    return None
