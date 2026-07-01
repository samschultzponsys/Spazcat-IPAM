# Spazcat IPAM

A lightweight, self-hosted IP Address Management tool. UniFi as standard stays your DHCP
source of truth; this is the planning + visualization layer on top of it —
color-coded pools, drag-and-drop device assignment, duplicate-IP detection, and
CSV export so you're never locked to one DHCP server.

Stack: Flask + SQLite + React (via CDN, no build step). Single container.

## Screenshots!
<img width="1094" height="834" alt="scipam1" src="https://github.com/user-attachments/assets/afbe6034-0c26-48e2-9d88-d5e6e419cc2a" />
<img width="1087" height="481" alt="scipam4" src="https://github.com/user-attachments/assets/f9a97237-8e1f-4a07-b4f1-efd6be8bd162" />
<img width="376" height="529" alt="scipam3" src="https://github.com/user-attachments/assets/9581fdc7-e55b-4643-9f97-725bed13f52f" />
<img width="1087" height="481" alt="scipam4" src="https://github.com/user-attachments/assets/f9a97237-8e1f-4a07-b4f1-efd6be8bd162" />
<img width="1134" height="700" alt="scipam2" src="https://github.com/user-attachments/assets/38ac2601-b03d-4bfb-8149-d2aaf49eb4a9" />
<img width="1094" height="834" alt="scipam1" src="https://github.com/user-attachments/assets/afbe6034-0c26-48e2-9d88-d5e6e419cc2a" />

# CSV Export Data
<img width="998" height="126" alt="scipam5" src="https://github.com/user-attachments/assets/ecc9d988-1d13-4ee2-953f-5f08222e1dda" />

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

## Repo layout
 
```
.
├── Dockerfile              # builds the image (COPY app/…)
├── compose.manual.yaml     # manual deploy: runtime-pip, bind-mounts ./app
├── compose.yaml            # image deploy: runs the prebuilt GHCR image
├── .github/workflows/      # Action that builds + pushes to GHCR
├── .gitignore              # excludes data/, *.db, fonts
└── app/
    ├── app.py
    ├── requirements.txt
    └── static/             # index.html, login.html, favicon.svg, fonts/
```
All deploy methods run off this one tree, so GitHub stays the single source of
truth. Everything is relative to the repo, so `git pull` + restart is the whole
update loop.

## Deploy
 
Clone the repo onto the host, then pick a method (all serve on port 20080).
Carrying over an existing instance: copy your existing `ipam.db` into `./data/`
before first start (it's gitignored, so it never came from the repo).
 
**A. Manual (no image build)** — edits are live on restart:
```bash
docker compose up -d
```
Uses `compose.manual.yaml`: stock python image, installs deps at start, bind-mounts
`./app`. To update: `git pull && docker compose restart`.
 
**B. Docker image (from GHCR)** — after the Action publishes the image:
```bash
# edit compose.image.yaml for your needs!
docker compose -f compose.yaml up -d
```
To update: `docker compose -f compose.yaml pull && … up -d`.
 
**C. Bare metal (no Docker at all)**:
```bash
cd app
pip install -r requirements.txt
IPAM_DB=./data/ipam.db python app.py
```
 
A `Dockerfile` is included for building the image yourself or via the Action.

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
`static/fonts/` include fonts of your choise update the html files as needed to reflect this.
