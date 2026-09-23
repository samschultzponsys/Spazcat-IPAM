# Spazcat IPAM

A lightweight, self-hosted IP Address Management tool. Your controller or router
(UniFi, TP-Link Omada, Alta Labs, OpenWrt, MikroTik, OPNsense, Pi-hole) stays the
DHCP source of truth. This is the planning and visualization layer on top of it:
color-coded pools, automatic sorting by subnet, drag-and-drop device assignment,
duplicate-IP detection, and CSV export so you're never locked to one DHCP server.

Stack: Flask + SQLite + React (via CDN, no build step). Single container.

## Screenshots!
<img width="1094" height="834" alt="scipam1" src="https://github.com/user-attachments/assets/afbe6034-0c26-48e2-9d88-d5e6e419cc2a" />
<img width="1087" height="481" alt="scipam4" src="https://github.com/user-attachments/assets/f9a97237-8e1f-4a07-b4f1-efd6be8bd162" />
<img width="376" height="529" alt="scipam3" src="https://github.com/user-attachments/assets/9581fdc7-e55b-4643-9f97-725bed13f52f" />
<img width="1134" height="700" alt="scipam2" src="https://github.com/user-attachments/assets/38ac2601-b03d-4bfb-8149-d2aaf49eb4a9" />

# CSV Export Data
<img width="998" height="126" alt="scipam5" src="https://github.com/user-attachments/assets/ecc9d988-1d13-4ee2-953f-5f08222e1dda" />

## Data & privacy

**Everything lives in one local SQLite file: `/data/ipam.db`**, bind-mounted to
`data/` next to the compose file. It holds your platform credentials, settings,
last-sync time, the session secret, and all devices, pools and exclusions.

**Your data carries over between containers.** `data/` is a volume, so pulling a
new image and recreating the container reuses it. Schema changes are additive
only: new tables, new columns, new settings. Before the app starts a new version
against an existing DB, it writes a consistent copy to `data/backups/`
(`ipam-v<old>-<timestamp>.db`) and keeps the newest 10. To roll back, stop the
container, copy a backup over `data/ipam.db`, and run the older image tag.

**The app only contacts:**
- the controller or router you configure, during sync;
- `ghcr.io`, every 6 hours, to read this image's tag list for the update dot. You
  can turn this off in Settings → Maintenance, or hard-disable it with
  `IPAM_UPDATE_CHECK=false`;
- PyPI at container start, and only with the manual (runtime-pip) compose.

There is no telemetry and no analytics. The browser loads React and Babel from
unpkg. Credentials are stored server-side and never sent to the browser: secret
fields always load blank.

Credentials and the session secret are stored in the DB in plaintext, protected by
filesystem permissions. That's standard for self-hosted tools, and the DB never
leaves your box. Keep `data/` off any shared or synced location.

## Repo layout

```
.
├── CHANGELOG.md            # release notes + THE version number (newest "## x.y")
├── Dockerfile              # builds the image (COPY app/… + CHANGELOG.md)
├── compose.yaml            # image deploy: runs the prebuilt GHCR image
├── compose.manual.yaml     # manual deploy: runtime-pip, bind-mounts ./app
├── .github/workflows/      # Action that builds + pushes to GHCR, tagged x.y
├── .gitignore              # excludes data/, *.db
└── app/
    ├── app.py              # Flask app: API, sync, pools, fonts, version
    ├── platforms.py        # one class per sync platform
    ├── requirements.txt
    └── static/             # index.html, login.html, favicon.svg
        └── fonts/          # ← upload font files here (baked into the image)
```
All deploy methods run from this one tree, so GitHub stays the single source of
truth.

## Deploy

Clone the repo onto the host, then pick a method (all serve on port 20080).
Carrying over an existing instance: copy your existing `ipam.db` into `./data/`
before first start (it's gitignored, so it never came from the repo).

**A. Docker image (from GHCR)**, recommended:
```bash
docker compose up -d                      # uses compose.yaml
```
To update: `docker compose pull && docker compose up -d`. Your `data/` folder is
reused and backed up automatically. You can also pin a version tag (`:1.1`)
instead of `:latest`.

**B. Manual (no image build)**. Edits take effect on restart:
```bash
docker compose -f compose.manual.yaml up -d
```
Uses the stock python image, installs deps at start, and bind-mounts `./app`.
To update: `git pull && docker compose -f compose.manual.yaml restart`.

**C. Bare metal (no Docker at all)**:
```bash
cd app
pip install -r requirements.txt
IPAM_DB=./data/ipam.db python app.py
```

## Sync platforms

Pick one in **⚙ → Platform**, fill in its fields, **Test connection**, then **Save**
and **Sync**. Only one platform is active at a time. After a switch, devices from the
old platform go offline and age out normally. Nothing is deleted.

| Platform | How it connects | What you need |
|---|---|---|
| **UniFi** | UniFi OS API key (`X-API-KEY`) | Network app → Settings → Integrations → Create API Key. Host e.g. `https://10.0.1.1`, site usually `default`. |
| **TP-Link Omada** | Omada Open API, client-credentials | Controller → Settings → Platform Integration → Open API → Add New App, mode **Client**, a role with site view access. Host e.g. `https://10.0.1.2:8043`, the site *name* (e.g. `Default`). The Omada ID is auto-detected. |
| **Alta Labs Route10** *(experimental)* | SSH (read-only) | Alta has no public API yet. The Route10 runs OpenWrt, so this reads its DHCP leases, static leases and neighbor table over SSH. Enable SSH on the Route10 and use a password or a pasted private key. |
| **OpenWrt** | SSH (read-only) | Same as above, for any OpenWrt router using dnsmasq. |
| **MikroTik RouterOS 7** | REST API (`/rest/ip/dhcp-server/lease`) | `www-ssl` service enabled and a user (a read-only group is enough). |
| **OPNsense** | REST API, key + secret | System → Access → Users → API keys. Supports ISC DHCPv4, Kea, and dnsmasq leases. |
| **Pi-hole v6** | REST API | Only if Pi-hole is your DHCP server. An app password is recommended. |
| **None** | — | Manual-only IPAM; the Sync button is hidden. |

"Verify TLS" is off by default for self-signed controller certs. Secret fields show
**✓ saved**. Leave a saved secret blank to keep it, or click *clear* to remove it.

Adding another platform is one class in `app/platforms.py`: declare its fields and
return `{mac: {ip, name, hostname, online, is_reserved, last_seen}}` from
`get_clients()`.

## Pools & automatic sorting

- A pool's **subnets** are CIDRs (`10.0.20.0/24`), comma-separated for several.
- **Put new devices into their matching pool** (Settings → Sync & pools, *on* by
  default): a new device goes straight to the pool whose subnet contains its IP.
  The most specific subnet wins, so a `/28` carved out of a `/24` claims its own
  devices. With no match, the device lands in Unallocated. Quick-add follows the
  same rule.
- **Re-sort every device on every sync** (*off* by default): each sync, manual or
  timed, moves every unlocked device into its matching pool. This overrides
  drag-and-drop, so **lock** (🔒) a device to keep it where you put it.
- **Sort all devices into matching pools now**: a one-off button for devices that
  existed before a pool was defined, or before you turned the setting on.
- Creating or editing a pool has a checkbox, on by default, to pull in its matching
  devices right away.
- Sorting never moves locked devices, devices with no matching pool, or stale
  devices (offline past the grace period and not reserved). Stale devices stay in
  Unallocated, so the grace-period fallback and auto-sort don't fight.

## Duplicate IPs & stale data

Controllers remember every client they have ever seen, along with the *last* IP it
had. An old phone MAC (private-address rotation), a retired device, or a reservation
you switched off can all "have" an address that something else holds today.

- The **duplicate banner** counts only live claims: devices that are online,
  reserved, locked or added manually. Two of those on one IP is a real conflict.
- An offline, non-reserved device whose IP is held (or more recently held) by
  someone else gets a muted **STALE** tag instead. It's history, not a conflict.
- UniFi reservations follow the "Use fixed IP" toggle. A disabled reservation no
  longer pins its old address, and a reservation removed on the controller clears
  here on the next sync.
- A device's *last seen* is the controller's own timestamp. The sync time is no
  longer used, so old history ages out.
- **Forget offline clients not seen for N days** (off by default) keeps old history
  out entirely. It never removes locked, reserved or manual devices, or devices with
  notes. A pruned device comes back if it reconnects.
- MACs are stored in one format (`aa:bb:cc:dd:ee:ff`), whatever the controller sends.

## Auto-sync

Settings → Sync & pools → **Auto-sync every N minutes / hours / days** (0 = manual
only). The timer runs server-side, so it keeps running with no browser open. If
the controller can't be reached, the app waits one full interval before retrying.

## Appearance: fonts & app name

- **App name**: the top-left wordmark, browser tab title and login page
  (Settings → Appearance).
- **Fonts**: pick separate fonts for the **logo**, the **headers** (pool names,
  dialog titles), and the **text** (device rows and everything else). Changes
  preview live.
- **Adding fonts**: upload `.otf` / `.ttf` / `.woff` / `.woff2` files to
  [`app/static/fonts/`](app/static/fonts/) in GitHub (*Add file → Upload files*).
  The Action bakes them into the image. For a quick test without a rebuild, drop
  files into `data/fonts/` on the host and reload. There's no HTML to edit: the
  server generates the `@font-face` rules. See
  [`app/static/fonts/readme.md`](app/static/fonts/readme.md).
- If a file with `ethnocentric` in its name is present, the logo defaults to it
  (the original wordmark).
- Theme (☀/🌙) and text size (A−/A+) are in the toolbar. All appearance settings are
  stored per instance, so every browser sees the same settings.

## Versions, changelog & updates

- Versions are always **`MAJOR.MINOR` with a single-digit minor**: `1.0`, `1.1` …
  `1.9`, then `2.0`.
- **[`CHANGELOG.md`](CHANGELOG.md) is the single source of truth.** Its newest
  `## x.y` heading is the version the app reports and the tag the Action publishes
  (`:latest`, `:x.y`, `:sha-…`). The build fails if the heading isn't `x.y`.
- In the app, the version shows in the **bottom-left corner**. Click it to open the
  changelog. The changelog also opens by itself the first time someone visits and
  again after an update (tracked per browser).
- **Update dot**: every 6 hours the server reads this image's tags on GHCR. When a
  higher `x.y` tag exists, a pulsing dot appears next to the version, and the
  changelog dialog shows the pull command. Forks can point this at their own image
  with `IPAM_UPDATE_IMAGE`.

**Cutting a release:** add a new `## x.y — YYYY-MM-DD` section at the top of
`CHANGELOG.md` with the notes, and push to `main`.

## Authentication (optional, for WAN exposure)

Off by default. Enable it with environment variables (see `compose.yaml`):

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
provided, the app logs a warning and stays open, so you can't lock yourself out.
Set a password to actually protect it. The login page shows your app name and
logo font. Only the app name, logo font and font files are visible before login.

## Other environment variables

| var | purpose |
|-----|---------|
| `IPAM_DB` | DB path (default `/data/ipam.db`); backups and drop-in fonts sit next to it |
| `PORT` | listen port (default `20080`) |
| `IPAM_UPDATE_CHECK` | `false` to hard-disable the GHCR update check |
| `IPAM_UPDATE_IMAGE` | image to check for updates (default `ghcr.io/samschultzponsys/spazcat-ipam`) |

## Behavior notes

- Pools sort by subnet (first CIDR); Unallocated is pinned on top.
- Online devices show stronger (pool-color accent); offline ones recede. **RES**
  reflects fixed-IP / static-lease assignments on the controller.
- **Lock** a device (🔒) to protect it from auto-sort and the grace-period fallback.
- Offline, non-reserved devices past the grace period (default 7 days) fall back to
  Unallocated, so the IP stays visible. They are never silently deleted.
- Deleting a synced device excludes it permanently, so sync won't re-add it. Restore
  it any time from Settings → Maintenance.
- Search matches ip / name / hostname / mac / notes, plus pool name, notes and
  subnet. IP search is octet-aware (`10.0.1.2` ≠ `10.0.1.200`).
- CSV export is included.
