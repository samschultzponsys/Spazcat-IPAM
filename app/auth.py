"""
Authentication for Spazcat IPAM.

Configured from the environment only - on purpose. If the auth rules lived in
the Settings UI, anyone reaching the app through a zone with `none` could
switch off auth for every other zone.

Methods (per zone, comma-separated; any one enabled method is enough):
    none    no login at all (cannot be combined with anything else)
    local   username + password form
    oidc    OpenID Connect (Authentik, Authelia, Keycloak, Pocket ID, Google...)
    token   a secret in the URL (?token=...) or an Authorization: Bearer header

Zones: every request is either LAN (client IP inside IPAM_LAN_NETWORKS) or
WAN (anything else). Behind a reverse proxy (Nginx Proxy Manager, Traefik,
Caddy...) the client IP comes from X-Forwarded-For, but only when the direct
peer is a trusted proxy - see client_ip().

    IPAM_AUTH_LAN            methods for LAN clients   (default: see below)
    IPAM_AUTH_WAN            methods for WAN clients   (default: see below)
    IPAM_LAN_NETWORKS        CIDRs that count as LAN   (default: private ranges)
    IPAM_TRUSTED_PROXIES     CIDRs allowed to set X-Forwarded-*  (default: private ranges)
    IPAM_PUBLIC_URL          external base URL, e.g. https://ipam.example.com

    local:  IPAM_AUTH_USER + IPAM_AUTH_PASSWORD | IPAM_AUTH_PASSWORD_HASH,
            and/or IPAM_AUTH_USERS="alice:<hash-or-password>,bob:<...>"
    token:  IPAM_AUTH_TOKENS="phone:<secret>,<secret2>"  IPAM_AUTH_TOKEN_PARAM (token)
    oidc:   IPAM_OIDC_ISSUER  IPAM_OIDC_CLIENT_ID  IPAM_OIDC_CLIENT_SECRET
            IPAM_OIDC_SCOPES ("openid profile email")  IPAM_OIDC_REDIRECT_URI
            IPAM_OIDC_AUTO_LOGIN (false)  IPAM_OIDC_BUTTON_TEXT ("Sign in with SSO")
            IPAM_OIDC_ALLOWED_USERS  IPAM_OIDC_ALLOWED_GROUPS  IPAM_OIDC_GROUPS_CLAIM (groups)

Defaults: a zone with no IPAM_AUTH_<ZONE> set falls back to the pre-1.2
behavior - `local` if IPAM_AUTH_ENABLED=true, otherwise `none`.
"""

import hmac
import ipaddress
import os
import threading
import time
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from flask import g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

METHODS = ("none", "local", "oidc", "token")
PRIVATE = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
DEFAULT_LAN = PRIVATE + ",169.254.0.0/16,fe80::/10"

# paths reachable without a session in every zone (login page and what it needs)
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/api/auth/config", "/favicon.ico",
                "/api/branding", "/api/fonts.css", "/healthz",
                "/auth/oidc/login", "/auth/oidc/callback"}
PUBLIC_PREFIXES = ("/static/", "/fonts/")


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _env_bool(name, default=False):
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


def _log(msg):
    print(f"[auth] {msg}", flush=True)


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


def _nets(raw):
    out = []
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            _log(f"WARNING: ignoring invalid network '{tok}'")
    return out


def _in(addr, nets):
    return addr is not None and any(addr.version == n.version and addr in n for n in nets)


class AuthConfig:
    def __init__(self):
        self.lan_nets = _nets(_env("IPAM_LAN_NETWORKS", DEFAULT_LAN))
        self.trusted = _nets(_env("IPAM_TRUSTED_PROXIES", PRIVATE))
        self.public_url = _env("IPAM_PUBLIC_URL").rstrip("/")
        self.errors = []

        # --- local users ---------------------------------------------------
        self.users = {}
        user = _env("IPAM_AUTH_USER", "admin") or "admin"
        pw_hash, pw = _env("IPAM_AUTH_PASSWORD_HASH"), os.environ.get("IPAM_AUTH_PASSWORD", "")
        if pw_hash or pw:
            self.users[user] = pw_hash or generate_password_hash(pw)
        for item in _env("IPAM_AUTH_USERS").split(","):
            if ":" not in item:
                continue
            name, secret = item.split(":", 1)
            name, secret = name.strip(), secret.strip()
            if name and secret:
                # werkzeug hashes look like "scrypt:32768:8:1$salt$hash"
                self.users[name] = secret if "$" in secret else generate_password_hash(secret)

        # --- URI / bearer tokens -------------------------------------------
        self.token_param = _env("IPAM_AUTH_TOKEN_PARAM", "token") or "token"
        self.tokens = []   # [(label, secret)]
        for i, item in enumerate(t for t in _env("IPAM_AUTH_TOKENS").split(",") if t.strip()):
            label, _, secret = item.strip().rpartition(":")
            secret = secret.strip()
            if len(secret) < 16:
                _log(f"WARNING: token #{i + 1} is shorter than 16 characters - use "
                     "`openssl rand -hex 24` for something unguessable")
            self.tokens.append((label.strip() or f"token{i + 1}", secret))

        # --- OIDC ----------------------------------------------------------
        self.oidc_issuer = _env("IPAM_OIDC_ISSUER").rstrip("/")
        self.oidc_discovery = _env("IPAM_OIDC_DISCOVERY_URL") or (
            f"{self.oidc_issuer}/.well-known/openid-configuration" if self.oidc_issuer else "")
        self.oidc_client_id = _env("IPAM_OIDC_CLIENT_ID")
        self.oidc_client_secret = _env("IPAM_OIDC_CLIENT_SECRET")
        self.oidc_scopes = _env("IPAM_OIDC_SCOPES", "openid profile email") or "openid profile email"
        self.oidc_redirect_uri = _env("IPAM_OIDC_REDIRECT_URI")
        self.oidc_auto = _env_bool("IPAM_OIDC_AUTO_LOGIN")
        self.oidc_button = _env("IPAM_OIDC_BUTTON_TEXT", "Sign in with SSO") or "Sign in with SSO"
        self.oidc_allowed_users = {u.strip().lower() for u in _env("IPAM_OIDC_ALLOWED_USERS").split(",") if u.strip()}
        self.oidc_allowed_groups = {x.strip() for x in _env("IPAM_OIDC_ALLOWED_GROUPS").split(",") if x.strip()}
        self.oidc_groups_claim = _env("IPAM_OIDC_GROUPS_CLAIM", "groups") or "groups"

        # --- zones ---------------------------------------------------------
        legacy = "local" if _env_bool("IPAM_AUTH_ENABLED") else "none"
        self.zones = {z: self._parse_zone(z, _env(f"IPAM_AUTH_{z.upper()}", legacy))
                      for z in ("lan", "wan")}
        # pre-1.2 behavior: IPAM_AUTH_ENABLED without a password stayed open
        if legacy == "local" and not self.users and not _env("IPAM_AUTH_LAN") and not _env("IPAM_AUTH_WAN"):
            _log("WARNING: IPAM_AUTH_ENABLED is set but no password was provided. Auth is DISABLED.")
            self.zones = {"lan": ["none"], "wan": ["none"]}
            self.errors = []

    def usable(self, m):
        if m == "local":
            return bool(self.users)
        if m == "token":
            return bool(self.tokens)
        if m == "oidc":
            return bool(self.oidc_discovery and self.oidc_client_id)
        return m == "none"

    def _parse_zone(self, zone, raw):
        want = [m.strip().lower() for m in raw.split(",") if m.strip()] or ["none"]
        bad = [m for m in want if m not in METHODS]
        if bad:
            self._err(f"{zone.upper()}: unknown auth method(s) {', '.join(bad)} "
                      f"(use {', '.join(METHODS)})")
        want = [m for m in dict.fromkeys(want) if m in METHODS]
        if "none" in want and len(want) > 1:
            self._err(f"{zone.upper()}: 'none' can't be combined with other methods - "
                      "ignoring 'none'")
            want = [m for m in want if m != "none"]
        missing = {"local": "IPAM_AUTH_PASSWORD / IPAM_AUTH_PASSWORD_HASH / IPAM_AUTH_USERS",
                   "token": "IPAM_AUTH_TOKENS",
                   "oidc": "IPAM_OIDC_ISSUER + IPAM_OIDC_CLIENT_ID"}
        ok = []
        for m in want:
            if self.usable(m):
                ok.append(m)
            else:
                self._err(f"{zone.upper()}: '{m}' is enabled but not configured (needs {missing[m]})")
        if want and not ok:
            # fail closed: never fall back to open because of a typo
            self._err(f"{zone.upper()}: no usable auth method - {zone.upper()} access is BLOCKED "
                      "until this is fixed")
        return ok

    def _err(self, msg):
        self.errors.append(msg)
        _log("ERROR: " + msg)

    def describe(self):
        for z in ("lan", "wan"):
            _log(f"{z.upper()}: {', '.join(self.zones[z]) or 'BLOCKED (misconfigured)'}")


CFG = AuthConfig()
_oauth = None
_fail_lock = threading.Lock()
_failures = {}   # ip -> [timestamps]
FAIL_LIMIT, FAIL_WINDOW = 10, 15 * 60


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
    peer = peer_ip()
    if not _in(peer, CFG.trusted):
        return peer
    hops = [h for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    addr = peer
    for h in reversed(hops):
        a = _ip(h)
        if a is None:
            break
        addr = a
        if not _in(a, CFG.trusted):
            return a
    if not hops:
        real = _ip(request.headers.get("X-Real-IP", ""))
        if real is not None:
            return real
    return addr


def via_proxy():
    return _in(peer_ip(), CFG.trusted) and bool(
        request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"))


def client_zone():
    return "lan" if _in(client_ip(), CFG.lan_nets) else "wan"


def zone_methods(zone=None):
    return CFG.zones[zone or client_zone()]


def external_base():
    """Base URL as the browser sees it (for the OIDC redirect URI)."""
    if CFG.public_url:
        return CFG.public_url
    if via_proxy():
        proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
        host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
        return f"{proto}://{host}"
    return request.host_url.rstrip("/")


def redirect_uri():
    return CFG.oidc_redirect_uri or f"{external_base()}/auth/oidc/callback"


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
    found = None
    for label, secret in CFG.tokens:
        # compare every token so timing doesn't reveal which one nearly matched
        if hmac.compare_digest(value.encode(), secret.encode()):
            found = label
    return found


def _sign_in(method, user):
    session.clear()
    session.permanent = True
    session["authed"] = True
    session["method"] = method
    session["user"] = user


def current():
    """(authed, method, user) for this request."""
    if getattr(g, "auth_via", None):
        return True, g.auth_via, g.auth_user
    if session.get("authed"):
        return True, session.get("method"), session.get("user")
    return False, None, None


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
        "can_logout": authed and method in ("local", "oidc", "token") and methods != ["none"],
    }


def security_report():
    """What the Settings → Security tab shows (no secrets)."""
    peer = peer_ip()
    warnings = list(CFG.errors)
    ip = client_ip()
    if peer and not via_proxy() and _in(peer, [ipaddress.ip_network("172.16.0.0/12")]) \
            and str(peer).endswith(".1"):
        warnings.append(f"Your connection arrives from {peer}, which looks like a Docker "
                        "gateway. If every client shows this address, Docker is hiding real "
                        "client IPs and WAN traffic could be treated as LAN. Put a reverse "
                        "proxy in front, or publish the port without the userland proxy.")
    if "none" in CFG.zones["wan"]:
        warnings.append("WAN has no authentication. Don't forward this app to the internet "
                        "until IPAM_AUTH_WAN is set.")
    return {
        **request_status(),
        "client_ip": str(ip) if ip else None,
        "peer_ip": str(peer) if peer else None,
        "via_proxy": via_proxy(),
        "lan_methods": CFG.zones["lan"],
        "wan_methods": CFG.zones["wan"],
        "lan_networks": [str(n) for n in CFG.lan_nets],
        "trusted_proxies": [str(n) for n in CFG.trusted],
        "local_users": sorted(CFG.users),
        "tokens": [label for label, _ in CFG.tokens],
        "token_param": CFG.token_param,
        "oidc": {
            "configured": CFG.usable("oidc"),
            "issuer": CFG.oidc_issuer or CFG.oidc_discovery,
            "client_id": CFG.oidc_client_id,
            "redirect_uri": redirect_uri() if CFG.usable("oidc") else None,
            "auto_login": CFG.oidc_auto,
            "button_text": CFG.oidc_button,
            "allowed_users": sorted(CFG.oidc_allowed_users),
            "allowed_groups": sorted(CFG.oidc_allowed_groups),
        },
        "public_url": CFG.public_url or None,
        "warnings": warnings,
    }


def _strip_token_url():
    parts = urlsplit(request.full_path if request.query_string else request.path)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != CFG.token_param]
    return urlunsplit(("", "", parts.path, urlencode(q), ""))


# ----------------------------------------------------------------------------
# Flask wiring
# ----------------------------------------------------------------------------

def init_app(app):
    global _oauth
    CFG.describe()
    if CFG.usable("oidc"):
        from authlib.integrations.flask_client import OAuth
        _oauth = OAuth(app)
        _oauth.register(
            "idp",
            server_metadata_url=CFG.oidc_discovery,
            client_id=CFG.oidc_client_id,
            client_secret=CFG.oidc_client_secret or None,
            client_kwargs={"scope": CFG.oidc_scopes, "code_challenge_method": "S256"},
        )

    @app.before_request
    def _gate():
        zone = client_zone()
        methods = zone_methods(zone)
        path = request.path

        # token auth: header for API/scripts, ?token= in the URL for browsers
        if "token" in methods:
            hdr = request.headers.get("Authorization", "")
            bearer = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
            bearer = bearer or request.headers.get("X-IPAM-Token", "").strip()
            label = _match_token(bearer)
            if label:
                g.auth_via, g.auth_user = "token", label
                return
            url_tok = request.args.get(CFG.token_param)
            if url_tok:
                ip = client_ip()
                if _rate_limited(ip):
                    return jsonify({"error": "too many attempts, try later"}), 429
                label = _match_token(url_tok)
                if label:
                    _sign_in("token", label)
                    if request.method == "GET" and not path.startswith("/api/"):
                        # drop the secret from the address bar / history
                        return redirect(_strip_token_url())
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
        zone = client_zone()
        methods = zone_methods(zone)
        return jsonify({
            "zone": zone,
            "methods": methods,
            "local": "local" in methods,
            "oidc": "oidc" in methods,
            "token": "token" in methods,
            "oidc_button": CFG.oidc_button,
            "blocked": not methods,
            "errors": CFG.errors if not methods else [],
        })

    @app.route("/api/login", methods=["POST"])
    def api_login():
        methods = zone_methods()
        if methods == ["none"]:
            return jsonify({"ok": True})
        if "local" not in methods:
            return jsonify({"ok": False, "error": "Password sign-in isn't enabled here"}), 403
        ip = client_ip()
        if _rate_limited(ip):
            return jsonify({"ok": False, "error": "Too many failed attempts - try again in 15 minutes"}), 429
        data = request.get_json(force=True, silent=True) or {}
        user = (data.get("username") or "").strip()
        pw = data.get("password") or ""
        stored = CFG.users.get(user)
        # always run one hash check so unknown users take as long as known ones
        ok = check_password_hash(stored or next(iter(CFG.users.values())), pw) and stored is not None
        if not ok:
            _record_failure(ip)
            return jsonify({"ok": False, "error": "Invalid username or password"}), 401
        _sign_in("local", user)
        return jsonify({"ok": True, "next": _safe_next(data.get("next"))})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True, "redirect": "/login?logged_out=1"})

    @app.route("/auth/oidc/login")
    def oidc_login():
        if "oidc" not in zone_methods() or _oauth is None:
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
        if "oidc" not in zone_methods() or _oauth is None:
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
        if CFG.oidc_allowed_groups and CFG.oidc_groups_claim not in claims:
            try:  # some IdPs only put groups on the userinfo endpoint
                claims.update(_oauth.idp.userinfo(token=token))
            except Exception:
                pass
        names = {str(claims.get(k, "")).lower() for k in ("email", "preferred_username", "sub")} - {""}
        groups = claims.get(CFG.oidc_groups_claim) or []
        groups = {groups} if isinstance(groups, str) else set(map(str, groups))
        if CFG.oidc_allowed_users or CFG.oidc_allowed_groups:
            if not (names & CFG.oidc_allowed_users) and not (groups & CFG.oidc_allowed_groups):
                _log(f"OIDC user {sorted(names)} not in allowed users/groups")
                return fail("Your account isn't allowed to use this app")
        nxt = session.get("oidc_next", "/")
        user = claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "sso"
        _sign_in("oidc", user)
        return redirect(_safe_next(nxt))


def login_redirect():
    """Where GET /login should go instead of rendering, or None to render it."""
    methods = zone_methods()
    if methods == ["none"]:
        return "/"
    authed, method, _ = current()
    if authed and method in methods:
        return _safe_next(request.args.get("next"))
    manual = any(request.args.get(k) for k in ("manual", "error", "logged_out"))
    if CFG.oidc_auto and "oidc" in methods and not manual:
        return "/auth/oidc/login?" + urlencode({"next": _safe_next(request.args.get("next"))})
    return None
