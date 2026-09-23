"""
Sync sources ("platforms") for Spazcat IPAM.

Every platform turns its controller / router into the same shape:

    { mac: {"mac", "name", "hostname", "ip", "is_reserved", "online", "last_seen"} }

- mac         lowercase, colon separated (normalize_mac)
- is_reserved True only when the controller currently holds a fixed IP /
              static lease for the client
- online      True when the client is connected / holds a live lease right now
- last_seen   unix seconds as reported by the controller (0 if unknown)

To add a platform: subclass Platform, declare FIELDS (each field key is also
the settings key it's stored under, so keep them prefixed with the platform
id), implement get_clients(), and register it in PLATFORMS.
"""

import re
import time

import requests


class PlatformError(Exception):
    pass


# kept so older imports / call sites keep working
UniFiError = PlatformError

_MAC_HEX = re.compile(r"[^0-9a-f]")


def normalize_mac(mac):
    """'AA-BB-CC-DD-EE-FF', 'aabb.ccdd.eeff', 'aa:bb:..' -> 'aa:bb:cc:dd:ee:ff'.
    Anything that isn't 12 hex digits is returned lowercased/stripped as-is."""
    raw = (mac or "").strip().lower()
    hexonly = _MAC_HEX.sub("", raw)
    if len(hexonly) == 12:
        return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))
    return raw


def to_unix(v):
    """Controller timestamps come as seconds, milliseconds, or nothing."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0
    if v > 1e12:  # milliseconds
        v /= 1000.0
    return int(v)


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _host_url(host, default_scheme="https"):
    host = (host or "").strip().rstrip("/")
    if host and not host.startswith("http"):
        host = f"{default_scheme}://{host}"
    return host


def _entry(mac, name="", hostname="", ip="", is_reserved=False, online=False, last_seen=0):
    return {
        "mac": mac, "name": name or "", "hostname": hostname or "", "ip": ip or "",
        "is_reserved": bool(is_reserved), "online": bool(online),
        "last_seen": to_unix(last_seen),
    }


def F(key, label, kind="text", placeholder="", default="", help=""):
    """Settings field descriptor, rendered by the frontend."""
    return {"key": key, "label": label, "kind": kind, "placeholder": placeholder,
            "default": default, "help": help, "secret": kind == "password"}


class Platform:
    id = ""
    label = ""
    FIELDS = []
    HELP = ""
    EXPERIMENTAL = False

    def __init__(self, cfg):
        self.cfg = cfg or {}

    def get(self, key, default=""):
        v = self.cfg.get(key)
        return default if v is None or v == "" else v

    def require(self, *keys):
        missing = [k for k in keys if not str(self.get(k, "")).strip()]
        if missing:
            labels = {f["key"]: f["label"] for f in self.FIELDS}
            raise PlatformError(
                f"{self.label} is not configured - missing: "
                + ", ".join(labels.get(k, k) for k in missing))

    def session(self, verify_key=None):
        s = requests.Session()
        s.verify = _truthy(self.get(verify_key, "0")) if verify_key else False
        return s

    def get_clients(self):
        raise NotImplementedError


# ----------------------------------------------------------------------------
# UniFi
# ----------------------------------------------------------------------------

class UniFiPlatform(Platform):
    """UniFi OS console (UDM / UCG / UDR / UX / UniFi OS Server) via a stateless
    API key (Network app -> Settings -> Integrations), sent as X-API-KEY. Uses
    the proxied classic Network API: stat/sta (connected clients) and rest/user
    (known clients + fixed-IP reservations)."""

    id = "unifi"
    label = "UniFi"
    FIELDS = [
        F("unifi_host", "Console host", placeholder="https://10.0.1.1 or unifi.yourdomain.com"),
        F("unifi_api_key", "API key", "password", placeholder="Network app → Settings → Integrations"),
        F("unifi_site", "Site", placeholder="default", default="default"),
        F("unifi_verify_ssl", "Verify TLS certificate (off for self-signed)", "checkbox", default="0"),
    ]
    HELP = "Network application → Settings → Integrations → Create API Key."

    def _get(self, s, path):
        host = _host_url(self.get("unifi_host"))
        site = self.get("unifi_site", "default")
        headers = {"X-API-KEY": self.get("unifi_api_key").strip(), "Accept": "application/json"}
        try:
            r = s.get(f"{host}/proxy/network/api/s/{site}{path}", headers=headers, timeout=15)
        except requests.RequestException as e:
            raise PlatformError(f"Connection failed: {e}")
        if r.status_code in (401, 403):
            raise PlatformError(
                "Auth rejected - check the API key (Network app -> Settings -> "
                "Integrations) and that the host is reachable.")
        if r.status_code != 200:
            raise PlatformError(f"API {path} returned HTTP {r.status_code}")
        try:
            return r.json().get("data", [])
        except ValueError:
            raise PlatformError(f"Unexpected non-JSON response from {path}")

    @staticmethod
    def _fixed(u):
        """(is_reserved, fixed_ip). UniFi keeps `fixed_ip` on the record after
        "Use fixed IP" is switched off, so `use_fixedip` decides when present -
        otherwise a disabled reservation keeps claiming its old IP."""
        fixed_ip = u.get("fixed_ip") or ""
        if "use_fixedip" in u:
            on = bool(u.get("use_fixedip")) and bool(fixed_ip)
        else:
            on = bool(fixed_ip)
        return on, (fixed_ip if on else "")

    def get_clients(self):
        self.require("unifi_host", "unifi_api_key")
        s = self.session("unifi_verify_ssl")
        active = self._get(s, "/stat/sta")   # currently connected
        known = self._get(s, "/rest/user")   # all known/configured (has fixed_ip)

        merged = {}
        for u in known:
            mac = normalize_mac(u.get("mac"))
            if not mac:
                continue
            res, fixed_ip = self._fixed(u)
            merged[mac] = _entry(
                mac, u.get("name"), u.get("hostname"),
                fixed_ip or u.get("last_ip") or "",
                res, False, u.get("last_seen"))
        for c in active:
            mac = normalize_mac(c.get("mac"))
            if not mac:
                continue
            e = merged.setdefault(mac, _entry(mac))
            e["online"] = True
            e["ip"] = c.get("ip") or e["ip"]
            e["hostname"] = c.get("hostname") or e["hostname"]
            e["name"] = c.get("name") or e["name"]
            res, fixed_ip = self._fixed(c)
            if res:
                e["is_reserved"] = True
                e["ip"] = fixed_ip or e["ip"]
            e["last_seen"] = to_unix(c.get("last_seen")) or e["last_seen"]
        return merged


# ----------------------------------------------------------------------------
# TP-Link Omada
# ----------------------------------------------------------------------------

class OmadaPlatform(Platform):
    """Omada Software Controller / OC200 / OC300 / Omada Cloud via the Open API
    (Settings -> Platform Integration -> Open API, "Client" mode app). Token
    from /openapi/authorize/token (client_credentials); header is
    `Authorization: AccessToken=<token>`."""

    id = "omada"
    label = "TP-Link Omada"
    FIELDS = [
        F("omada_host", "Controller URL", placeholder="https://10.0.1.2:8043"),
        F("omada_client_id", "Client ID"),
        F("omada_client_secret", "Client secret", "password"),
        F("omada_site", "Site name", placeholder="Default", default="Default"),
        F("omada_omadac_id", "Omada ID (optional — auto-detected)", placeholder="auto"),
        F("omada_verify_ssl", "Verify TLS certificate (off for self-signed)", "checkbox", default="0"),
    ]
    HELP = ("Controller → Settings → Platform Integration → Open API → Add New App, "
            "mode \"Client\", role with Site viewer access. Copy the Client ID / Secret.")

    def _json(self, r, what):
        if r.status_code in (401, 403):
            raise PlatformError(f"{what}: auth rejected (HTTP {r.status_code})")
        if r.status_code != 200:
            raise PlatformError(f"{what}: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise PlatformError(f"{what}: unexpected non-JSON response")
        if data.get("errorCode", 0) != 0:
            raise PlatformError(f"{what}: {data.get('msg') or 'error'} ({data.get('errorCode')})")
        return data.get("result")

    def get_clients(self):
        self.require("omada_host", "omada_client_id", "omada_client_secret")
        host = _host_url(self.get("omada_host"))
        s = self.session("omada_verify_ssl")
        try:
            cid = self.get("omada_omadac_id").strip()
            if not cid or cid == "auto":
                info = self._json(s.get(f"{host}/api/info", timeout=15), "Controller info")
                cid = (info or {}).get("omadacId") or ""
                if not cid:
                    raise PlatformError("Could not auto-detect the Omada ID; enter it manually.")
            tok = self._json(s.post(
                f"{host}/openapi/authorize/token", params={"grant_type": "client_credentials"},
                json={"omadacId": cid, "client_id": self.get("omada_client_id").strip(),
                      "client_secret": self.get("omada_client_secret").strip()},
                timeout=15), "Token")
            s.headers["Authorization"] = f"AccessToken={tok['accessToken']}"

            base = f"{host}/openapi/v1/{cid}"
            sites = self._json(s.get(f"{base}/sites", params={"page": 1, "pageSize": 1000},
                                     timeout=15), "Sites") or {}
            want = self.get("omada_site", "Default").strip().lower()
            site = next((x for x in sites.get("data", [])
                         if (x.get("name") or "").lower() == want
                         or (x.get("siteId") or x.get("id")) == want), None)
            if not site:
                names = ", ".join(x.get("name", "?") for x in sites.get("data", []))
                raise PlatformError(f"Site '{want}' not found. Available: {names or 'none'}")
            sid = site.get("siteId") or site.get("id")

            rows = self._clients(s, host, cid, sid)
        except requests.RequestException as e:
            raise PlatformError(f"Connection failed: {e}")

        out = {}
        for c in rows:
            mac = normalize_mac(c.get("mac"))
            if not mac:
                continue
            fixed = c.get("ipSetting") or {}
            reserved = bool(c.get("fixedIp") or fixed.get("useFixedAddr"))
            ip = (fixed.get("netIpAddr") if fixed.get("useFixedAddr") else None) or c.get("ip") or ""
            out[mac] = _entry(mac, c.get("name"), c.get("hostName"), ip, reserved,
                              c.get("active", True), c.get("lastSeen"))
        return out

    def _clients(self, s, host, cid, sid):
        """v2 POST (scope 0 = online + offline), falling back to v1 GET
        (online only) on controllers that don't have v2."""
        rows, page = [], 1
        try:
            while True:
                res = self._json(s.post(
                    f"{host}/openapi/v2/{cid}/sites/{sid}/clients",
                    json={"page": page, "pageSize": 1000, "scope": 0, "filters": {}},
                    timeout=30), "Clients") or {}
                rows += res.get("data", [])
                if len(rows) >= int(res.get("totalRows") or 0) or not res.get("data"):
                    return rows
                page += 1
        except PlatformError:
            if rows:
                raise
        rows, page = [], 1
        while True:
            res = self._json(s.get(
                f"{host}/openapi/v1/{cid}/sites/{sid}/clients",
                params={"page": page, "pageSize": 1000}, timeout=30), "Clients") or {}
            rows += res.get("data", [])
            if len(rows) >= int(res.get("totalRows") or 0) or not res.get("data"):
                return rows
            page += 1


# ----------------------------------------------------------------------------
# OpenWrt / Alta Labs Route10 (SSH)
# ----------------------------------------------------------------------------

_SSH_SCRIPT = r"""
echo '#LEASES'; cat /tmp/dhcp.leases 2>/dev/null; cat /tmp/hosts/odhcpd 2>/dev/null
echo '#STATIC'; uci -q show dhcp 2>/dev/null | grep -E "=host$|\.(mac|ip|name)="
echo '#NEIGH'; ip -4 neigh show 2>/dev/null
"""


class OpenWrtSSHPlatform(Platform):
    """Any OpenWrt-based router over SSH (Alta Labs Route10 is OpenWrt under
    the hood and has no public API yet). Reads dnsmasq leases
    (/tmp/dhcp.leases), static leases (uci dhcp host sections) and the ARP /
    neighbor table for online state. Read-only: nothing is changed."""

    id = "openwrt"
    label = "OpenWrt (SSH)"
    PREFIX = "openwrt"
    HELP = ("Uses SSH to read DHCP leases, static leases and the neighbor table. "
            "Read-only. Password or private key (paste the key text).")

    @classmethod
    def fields(cls):
        p = cls.PREFIX
        return [
            F(f"{p}_host", "Router host / IP", placeholder="10.0.1.1"),
            F(f"{p}_port", "SSH port", "number", placeholder="22", default="22"),
            F(f"{p}_user", "SSH user", placeholder="root", default="root"),
            F(f"{p}_password", "SSH password", "password", help="Leave blank if using a key"),
            F(f"{p}_key", "SSH private key (optional)", "password",
              placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"),
        ]

    def __init__(self, cfg):
        super().__init__(cfg)
        self.FIELDS = self.fields()

    def _run(self):
        try:
            import io
            import paramiko
        except ImportError:
            raise PlatformError("SSH support needs the 'paramiko' package (pip install paramiko).")
        p = self.PREFIX
        self.require(f"{p}_host", f"{p}_user")
        pkey = None
        keytxt = self.get(f"{p}_key").strip()
        if keytxt:
            for kc in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    pkey = kc.from_private_key(io.StringIO(keytxt))
                    break
                except Exception:
                    continue
            if pkey is None:
                raise PlatformError("Could not parse the SSH private key.")
        cli = paramiko.SSHClient()
        # home-lab routers are addressed by IP with keys that change on reflash;
        # this is a read-only session so accept the host key rather than fail.
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            cli.connect(self.get(f"{p}_host").strip(), port=int(self.get(f"{p}_port", "22") or 22),
                        username=self.get(f"{p}_user", "root"),
                        password=self.get(f"{p}_password") or None, pkey=pkey,
                        timeout=15, banner_timeout=15, auth_timeout=15,
                        look_for_keys=False, allow_agent=False)
            _, stdout, _ = cli.exec_command(_SSH_SCRIPT, timeout=30)
            return stdout.read().decode("utf-8", "replace")
        except paramiko.AuthenticationException:
            raise PlatformError("SSH auth rejected - check user / password / key.")
        except Exception as e:
            raise PlatformError(f"SSH failed: {e}")
        finally:
            cli.close()

    @staticmethod
    def parse(text, now=None):
        now = now or int(time.time())
        section, out = None, {}
        static = {}   # uci section -> {mac, ip, name}
        online = set()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") and line[1:] in ("LEASES", "STATIC", "NEIGH"):
                section = line[1:]
                continue
            if not line:
                continue
            if section == "LEASES":
                # dnsmasq: <expiry> <mac> <ip> <hostname|*> <client-id|*>
                parts = line.split()
                if len(parts) >= 4 and ":" in parts[1]:
                    mac = normalize_mac(parts[1])
                    host = "" if parts[3] == "*" else parts[3]
                    out[mac] = _entry(mac, "", host, parts[2], False, True, now)
            elif section == "STATIC":
                m = re.match(r"dhcp\.([^.=]+)\.(mac|ip|name)='?(.*?)'?$", line)
                if m:
                    static.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
            elif section == "NEIGH":
                # 10.0.1.5 dev br-lan lladdr aa:bb:.. REACHABLE
                parts = line.split()
                if "lladdr" in parts:
                    mac = normalize_mac(parts[parts.index("lladdr") + 1])
                    state = parts[-1].upper()
                    if state in ("REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"):
                        online.add(mac)
                        e = out.get(mac)
                        if e and not e["ip"]:
                            e["ip"] = parts[0]
        for h in static.values():
            for raw in (h.get("mac") or "").split():
                mac = normalize_mac(raw)
                if not mac:
                    continue
                e = out.setdefault(mac, _entry(mac))
                e["is_reserved"] = bool(h.get("ip"))
                e["ip"] = h.get("ip") or e["ip"]
                e["name"] = h.get("name") or e["name"]
        for mac, e in out.items():
            # a lease alone can outlive the client; the neighbor table is the
            # better "is it actually here" signal when the router has it
            if online:
                e["online"] = mac in online
                # 0 = unknown: the sync keeps the last time it saw the device
                # online, so a lingering lease doesn't look fresh forever
                e["last_seen"] = now if e["online"] else 0
        return out

    def get_clients(self):
        return self.parse(self._run())


class AltaRoute10Platform(OpenWrtSSHPlatform):
    id = "alta"
    label = "Alta Labs Route10 (SSH)"
    PREFIX = "alta"
    EXPERIMENTAL = True
    HELP = ("Alta Labs has no public API yet, so this reads the Route10's OpenWrt "
            "DHCP state over SSH (read-only). Enable SSH on the Route10 first.")


# ----------------------------------------------------------------------------
# MikroTik RouterOS 7 (REST)
# ----------------------------------------------------------------------------

class MikroTikPlatform(Platform):
    """RouterOS v7 REST API (www-ssl service): GET /rest/ip/dhcp-server/lease."""

    id = "mikrotik"
    label = "MikroTik RouterOS 7"
    FIELDS = [
        F("mikrotik_host", "Router URL", placeholder="https://10.0.1.1"),
        F("mikrotik_user", "Username", placeholder="ipam-read"),
        F("mikrotik_password", "Password", "password"),
        F("mikrotik_verify_ssl", "Verify TLS certificate (off for self-signed)", "checkbox", default="0"),
    ]
    HELP = "Needs the www-ssl (or www) service enabled. A read-only user group is enough."

    def get_clients(self):
        self.require("mikrotik_host", "mikrotik_user")
        s = self.session("mikrotik_verify_ssl")
        s.auth = (self.get("mikrotik_user"), self.get("mikrotik_password"))
        try:
            r = s.get(f"{_host_url(self.get('mikrotik_host'))}/rest/ip/dhcp-server/lease", timeout=15)
        except requests.RequestException as e:
            raise PlatformError(f"Connection failed: {e}")
        if r.status_code in (401, 403):
            raise PlatformError("Auth rejected - check username / password.")
        if r.status_code != 200:
            raise PlatformError(f"RouterOS returned HTTP {r.status_code}")
        try:
            rows = r.json()
        except ValueError:
            raise PlatformError("Unexpected non-JSON response from RouterOS")
        now = int(time.time())
        out = {}
        for x in rows:
            mac = normalize_mac(x.get("mac-address") or x.get("active-mac-address"))
            if not mac or _truthy(x.get("disabled")):
                continue
            online = x.get("status") == "bound"
            out[mac] = _entry(mac, x.get("comment"), x.get("host-name"),
                              x.get("address") or x.get("active-address"),
                              not _truthy(x.get("dynamic")), online, now if online else 0)
        return out


# ----------------------------------------------------------------------------
# OPNsense (REST)
# ----------------------------------------------------------------------------

class OPNsensePlatform(Platform):
    """OPNsense API key + secret (HTTP basic). Tries ISC DHCPv4, Kea, then
    dnsmasq lease endpoints - whichever DHCP server is in use answers."""

    id = "opnsense"
    label = "OPNsense"
    FIELDS = [
        F("opnsense_host", "Firewall URL", placeholder="https://10.0.1.1"),
        F("opnsense_key", "API key"),
        F("opnsense_secret", "API secret", "password"),
        F("opnsense_verify_ssl", "Verify TLS certificate (off for self-signed)", "checkbox", default="0"),
    ]
    HELP = "System → Access → Users → (user) → API keys → + to download a key/secret pair."

    ENDPOINTS = ("/api/dhcpv4/leases/searchLease", "/api/kea/leases4/search",
                 "/api/dnsmasq/leases/search")

    def get_clients(self):
        self.require("opnsense_host", "opnsense_key", "opnsense_secret")
        s = self.session("opnsense_verify_ssl")
        s.auth = (self.get("opnsense_key").strip(), self.get("opnsense_secret").strip())
        host = _host_url(self.get("opnsense_host"))
        rows, last = None, "no lease endpoint answered"
        for ep in self.ENDPOINTS:
            try:
                r = s.get(f"{host}{ep}", params={"rowCount": -1}, timeout=15)
            except requests.RequestException as e:
                raise PlatformError(f"Connection failed: {e}")
            if r.status_code in (401, 403):
                raise PlatformError("Auth rejected - check the API key / secret.")
            if r.status_code == 200:
                try:
                    rows = r.json().get("rows")
                except ValueError:
                    rows = None
                if rows:
                    break
            last = f"{ep} -> HTTP {r.status_code}"
        if rows is None:
            raise PlatformError(f"Could not read leases ({last})")
        now = int(time.time())
        out = {}
        for x in rows:
            mac = normalize_mac(x.get("mac") or x.get("hwaddr"))
            if not mac:
                continue
            online = (x.get("status") == "online") or (
                "status" not in x and x.get("state", "active") in ("active", "0", 0))
            out[mac] = _entry(mac, x.get("descr") or x.get("description"),
                              x.get("hostname") or x.get("client_hostname"),
                              x.get("address") or x.get("ip"),
                              x.get("type") == "static", online, now if online else 0)
        return out


# ----------------------------------------------------------------------------
# Pi-hole v6 (REST)
# ----------------------------------------------------------------------------

class PiholePlatform(Platform):
    """Pi-hole v6 as DHCP server: /api/dhcp/leases + static hosts from
    /api/config/dhcp/hosts. Session via POST /api/auth (app password works)."""

    id = "pihole"
    label = "Pi-hole v6 (DHCP)"
    FIELDS = [
        F("pihole_host", "Pi-hole URL", placeholder="http://10.0.1.3"),
        F("pihole_password", "Password / app password", "password"),
        F("pihole_verify_ssl", "Verify TLS certificate", "checkbox", default="0"),
    ]
    HELP = "Only useful when Pi-hole is your DHCP server. An app password is recommended."

    def get_clients(self):
        self.require("pihole_host")
        host = _host_url(self.get("pihole_host"), "http")
        s = self.session("pihole_verify_ssl")
        sid = None
        try:
            r = s.post(f"{host}/api/auth", json={"password": self.get("pihole_password")}, timeout=15)
            if r.status_code == 401:
                raise PlatformError("Auth rejected - check the password.")
            if r.status_code != 200:
                raise PlatformError(f"Pi-hole auth returned HTTP {r.status_code}")
            sid = (r.json().get("session") or {}).get("sid")
            if sid:
                s.headers["X-FTL-SID"] = sid
            leases = s.get(f"{host}/api/dhcp/leases", timeout=15).json().get("leases", [])
            hosts = (((s.get(f"{host}/api/config/dhcp/hosts", timeout=15).json()
                       .get("config") or {}).get("dhcp") or {}).get("hosts") or [])
        except requests.RequestException as e:
            raise PlatformError(f"Connection failed: {e}")
        except ValueError:
            raise PlatformError("Unexpected non-JSON response from Pi-hole")
        finally:
            if sid:
                try:
                    s.delete(f"{host}/api/auth", timeout=5)
                except requests.RequestException:
                    pass
        now = int(time.time())
        out = {}
        for x in leases:
            mac = normalize_mac(x.get("hwaddr"))
            if mac:
                name = "" if x.get("name") == "*" else x.get("name")
                out[mac] = _entry(mac, "", name, x.get("ip"), False, True, now)
        # dnsmasq dhcp-host syntax: "mac[,mac..],ip,name[,lease]"
        for h in hosts:
            parts = [p.strip() for p in str(h).split(",")]
            macs = [normalize_mac(p) for p in parts if re.fullmatch(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", p)]
            ips = [p for p in parts if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p)]
            names = [p for p in parts if p and normalize_mac(p) not in macs and p not in ips
                     and not re.fullmatch(r"\d+[smhdw]?|infinite", p)]
            for mac in macs:
                e = out.setdefault(mac, _entry(mac))
                e["is_reserved"] = bool(ips)
                e["ip"] = (ips[0] if ips else "") or e["ip"]
                e["name"] = (names[0] if names else "") or e["name"]
        return out


class ManualPlatform(Platform):
    id = "none"
    label = "None (manual only)"
    FIELDS = []
    HELP = "No controller. Add and manage devices by hand; Sync is disabled."

    def get_clients(self):
        raise PlatformError("No sync platform selected (Settings → Platform).")


PLATFORMS = {p.id: p for p in (
    UniFiPlatform, OmadaPlatform, AltaRoute10Platform, OpenWrtSSHPlatform,
    MikroTikPlatform, OPNsensePlatform, PiholePlatform, ManualPlatform,
)}


def platform_fields(cls):
    return cls.fields() if hasattr(cls, "fields") else cls.FIELDS


def platform_catalog():
    """Metadata the settings UI renders the platform dropdown / form from."""
    return [{"id": c.id, "label": c.label, "help": c.HELP,
             "experimental": c.EXPERIMENTAL, "fields": platform_fields(c)}
            for c in PLATFORMS.values()]


def all_fields():
    seen = {}
    for c in PLATFORMS.values():
        for f in platform_fields(c):
            seen[f["key"]] = f
    return seen


def make_platform(pid, cfg):
    cls = PLATFORMS.get(pid)
    if not cls:
        raise PlatformError(f"Unknown platform '{pid}'")
    return cls(cfg)
