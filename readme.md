# Spazcat IPAM

A lightweight, self-hosted IP Address Management tool. UniFi stays your DHCP
source of truth; this is the planning + visualization layer on top of it —
color-coded pools, drag-and-drop device assignment, duplicate-IP detection, and
CSV export so you're never locked to one DHCP server.

Stack: Flask + SQLite + React (via CDN, no build step). Single container.

## Data & privacy

**Everything lives in one local SQLite file: `/data/ipam.db`** (bind-mounted to
`data/` next to the compose file). That includes your UniFi API key, host/site,
grace period, auto-sync interval, theme, font size, last-sync time, the session
secret, and all devices / pools / exclusions.

**Nothing is sent anywhere except your own infrastructure.** The app makes
outbound calls only to (a) the UniFi controller you configure, during sync, and
(b) PyPI at container start, only with the runtime-pip compose, to install
Flask. No telemetry, analytics, or third-party calls. The UniFi API key is
stored server-side and never returned to the browser (the field loads blank).

The API key and session secret are stored in the DB in plaintext, protected by
filesystem permissions — standard for self-hosted tools, and the DB never leaves
your box. Keep `data/` off any shared/synced location.

## Deploy (no Dockerfile)

```bash
mkdir -p /path/to/spazcat-ipam/{app,data}
# copy app.py + static/ into .../spazcat-ipam/app/
docker compose up -d
```

Open `http://<host>:20080`. Edit `app.py`/`static/index.html` on the host and
`docker restart` to iterate. A `Dockerfile` + `requirements.txt` are included for
building a real image (e.g. GHCR) later.

## UniFi connection (API key)

Modern UniFi OS (Network app 10.1.84+) uses a stateless API key.

1. Network application → Settings → Integrations → Create API Key. Copy it.
2. In this app: ⚙ → console host (e.g. `https://10.0.1.1`), paste the key,
   site is usually `default`, leave "Verify TLS" off for the self-signed cert.
3. Test connection, Save, then Sync.

## Authentication (optional — for WAN exposure)

Off by default. Enable via environment variables (see `compose.yaml`):

| var | purpose |
|-----|---------|
| `IPAM_AUTH_ENABLED` | `true` to require login |
| `IPAM_AUTH_USER` | username (default `admin`) |
| `IPAM_AUTH_PASSWORD` | plaintext password, hashed (scrypt) in memory at boot |
| `IPAM_AUTH_PASSWORD_HASH` | pre-hashed password; wins if set, keeps plaintext out of compose |
| `IPAM_SESSION_DAYS` | login lifetime, default 30 |
| `IPAM_COOKIE_SECURE` | `true` when served over HTTPS |
| `IPAM_SECRET_KEY` | optional; else a stable secret is generated and stored in the DB |

Generate a password hash (so no plaintext lives in your compose):

```bash
python -c "from werkzeug.security import generate_password_hash as g; print(g('yourpassword'))"
```

Passwords are verified with a KDF (scrypt) and never stored in plaintext.
Sessions are signed, HttpOnly cookies. If auth is enabled but no password is
provided, the app logs a warning and stays open (so you can't lock yourself out)
— set a password to actually protect it.

## Behavior notes

- Pools sort by subnet (first CIDR); Unallocated is pinned on top. Subnets must
  be CIDR (`10.0.20.0/24`), comma-separated for multiple.
- Online devices show stronger (pool-color accent); offline recede. RES reflects
  UniFi fixed-IP assignments.
- Lock a device (🔒) to protect it from the grace-period fallback.
- Offline devices grey out and, after the grace period (default 7 days), unlocked
  ones fall back to Unallocated so the IP stays visible — never silently deleted.
- Deleting a UniFi device excludes it permanently (won't be re-added by sync);
  restore anytime from Settings → Excluded devices.
- Search matches ip / name / hostname / mac / notes + pool name/notes/subnet.
  IP search is octet-aware (`10.0.1.2` ≠ `10.0.1.200`).
- Auto-sync interval, grace period, theme, and font size are per-instance
  (stored server-side), so every browser/device sees the same settings.
- Duplicate IPs are flagged inline and in a top banner. CSV export included.

## Fonts

Self-hosted. Drop `ethnocentric rg.otf` / `ethnocentric rg it.otf` into
`static/fonts/` (see `static/fonts/README.txt`). Falls back to a mono stack.
Font files are gitignored — don't commit licensed fonts.

## Tests

```bash
python test_ipam.py    # CRUD, pools, CSV, settings, exclusion, lock, CIDR
python test_sync.py    # sync grace/grey/move-to-Unallocated + exclusion lifecycle
```

## Committing to GitHub

The included `.gitignore` excludes `data/`, all `*.db`, font files, and
`__pycache__`. Only code is committed — no personal data, keys, or fonts. The DB
is created fresh at runtime on whatever box runs it.
