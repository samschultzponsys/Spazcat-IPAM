# Spazcat IPAM

A lightweight, self-hosted IP Address Management tool. Your controller or router
(UniFi, TP-Link Omada, Alta Labs, OpenWrt, MikroTik, OPNsense, Pi-hole, AdGuard Home,
Technitium) stays the DHCP source of truth. This is the planning and visualization
layer on top of it: color-coded pools, automatic sorting by subnet, drag-and-drop
device assignment, duplicate-IP detection, and CSV export so you're never locked to
one DHCP server.

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
- your OIDC provider, if you use SSO sign-in.

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
├── Dockerfile              # image recipe, built by the Action (not locally)
├── compose.yaml            # deploy: runs the prebuilt GHCR image
├── .github/workflows/      # Action that builds + pushes to GHCR, tagged x.y
├── .gitignore              # excludes data/, *.db
└── app/
    ├── app.py              # Flask app: API, sync, pools, fonts, version
    ├── auth.py             # sign-in: LAN/WAN zones, local, OIDC, tokens
    ├── platforms.py        # one class per sync platform
    ├── requirements.txt
    └── static/             # index.html, login.html, favicon.svg
        └── fonts/          # ← upload font files here (baked into the image)
```
GitHub is the single source of truth: every push to `main` builds and publishes
the image, and that image is the only supported way to run the app.

## Deploy

Spazcat IPAM ships as a prebuilt container image
(`ghcr.io/samschultzponsys/spazcat-ipam`, amd64 + arm64). Grab
[`compose.yaml`](compose.yaml), put it in a folder on the host, and:

```bash
docker compose up -d          # serves on port 20080
```

To update: `docker compose pull && docker compose up -d`. Your `data/` folder is
reused and backed up automatically. You can pin a version tag (`:1.2`) instead of
`:latest`. To carry over an existing instance, copy its `ipam.db` into `./data/`
before first start.

The container has a health check (`/healthz`) and runs on the Waitress production
web server. There's no local-build or bare-metal path. Changes go through GitHub,
and the Action builds the image.

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
| **AdGuard Home** | REST API, basic auth | Uses AdGuard's DHCP leases and static leases when it's your DHCP server. Persistent clients (Settings → Client settings) that list both a MAC and an IP are imported too, with their names, so a DNS-only AdGuard still adds the devices you've named there. Clients identified only by IP are skipped because devices are tracked by MAC. |
| **Technitium DNS** | REST API, token or user/password | Reads leases from Technitium's DHCP server. Reserved leases show as RES. An API token is recommended (Administration → Sessions → Create Token). |
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

## Pool settings

Each pool's **edit** dialog has a live preview of its header, plus:

- **Color**: 40 presets across bright, deep and pastel shades, a color wheel, or
  type any hex value.

- **Hide this pool while it has no devices.** A hidden pool shows again as soon as
  anything is in it. That includes Unallocated, which reappears the moment a device
  lands there. It also shows while you drag a device, so it still works as a drop
  target. A "N empty pools hidden — show" link under the search bar reveals hidden
  pools so you can edit them.
- **Header style:**
  - font (any installed font, or the global header font from Appearance)
  - size, bold, italic, UPPERCASE and letter spacing
  - name color and header tint strength
  - whether the color dot, subnets, notes and device count show

  *Reset style* returns the header to the defaults.
- The **▼ caret** on each header collapses the pool. This is remembered per
  browser, and a collapsed pool still accepts dropped devices.

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

## Authentication

### First start

Sign-in is **on by default**. The first time the container starts, it creates the
user `admin` with a random password and prints it in a box in the log:

```bash
docker logs spazcat-ipam 2>&1 | grep -A4 "ADMIN SIGN-IN"
```
```
  ╔══════════════════════════════════════════════════════╗
  ║            SPAZCAT IPAM  -  ADMIN SIGN-IN            ║
  ╠══════════════════════════════════════════════════════╣
  ║     username :  admin                                ║
  ║     password :  Nx7e-zfgu-Dejp-P9Fk                  ║
  ...
```

Sign in, then change the password in **Settings → Security**. A banner in the app
reminds you. Until you change it, the box is printed on every start, so you can
still find it after the container is recreated.

### Settings → Security

Everything is configured in the app:

- **Who needs to sign in**, separately for **LAN** (client IP inside your LAN
  networks; default: the private ranges) and **WAN** (everything else). Tick any
  combination of *Username + password*, *SSO (OIDC)* and *Access link / token*, and
  **any one** of them signs you in. *No sign-in* can't be combined with others.
- **Users.** Add users, change passwords, delete users. Scripts can use HTTP Basic
  auth with these users.
- **Access tokens.** Create one with a label and you get a ready-made link
  (`https://ipam.example.com/?token=…`) plus the header form
  (`Authorization: Bearer …`). The token is shown **once** and stored hashed.
  Opening the link signs the browser in and removes the token from the address bar.
  Revoke a token any time.
- **SSO (OIDC).** Issuer, client ID and secret, login button text, optional
  auto sign-in, and allowed users or groups. **Test provider** checks the discovery
  document, and the tab shows the redirect URI to register at your provider.
- **Networks & reverse proxy.** LAN networks, trusted proxies, public URL, session
  length and HTTPS-only cookie.
- The top of the tab shows the IP and zone the app sees for you, with warnings for
  risky setups.

Safety rails:
- **Only someone signed in with a password or SSO can change these settings**, not
  a visitor on a *No sign-in* network and not a token session. On an open LAN, the
  tab has a **Sign in to edit** link.
- A save that would lock *you* out, such as dropping the method you're signed in
  with on the network you're on, needs an explicit confirmation. A method that isn't
  set up yet (for example SSO with no issuer) can't be enabled.
- You can't delete your own user, the last user while password sign-in is on, or
  the last token while access links are on.
- A session only counts in a zone that allows the method it was created with. For
  example, a password login made at home doesn't carry over to a WAN that is set to
  SSO only.

Examples of common setups:

| LAN | WAN | use |
|---|---|---|
| No sign-in | SSO + password | open at home, SSO (or password fallback) from outside |
| Password | SSO, with auto sign-in | always sign in; from outside go straight to Authentik |
| No sign-in | Access link | a wall tablet with a bookmarked link |

### Environment overrides & recovery

Every setting can also be set with an environment variable in `compose.yaml`.
**An environment variable wins over the UI**, and the field shows as locked in
Settings → Security. Use these for infrastructure-as-code, or to get back in when
the UI config is broken.

| var | overrides |
|---|---|
| `IPAM_AUTH_LAN` / `IPAM_AUTH_WAN` | methods, e.g. `none` or `local,oidc` (`none`, `local`, `oidc`, `token`) |
| `IPAM_AUTH_USER` + `IPAM_AUTH_PASSWORD` or `IPAM_AUTH_PASSWORD_HASH` | adds or overrides one user (default name `admin`) while set |
| `IPAM_AUTH_USERS` | more users: `alice:<hash-or-password>,bob:<...>` |
| `IPAM_AUTH_TOKENS` | extra tokens: `label:SECRET,...`; `IPAM_AUTH_TOKEN_PARAM` renames `?token=` |
| `IPAM_LAN_NETWORKS`, `IPAM_TRUSTED_PROXIES`, `IPAM_PUBLIC_URL` | networks / proxy |
| `IPAM_OIDC_ISSUER`, `IPAM_OIDC_DISCOVERY_URL`, `IPAM_OIDC_CLIENT_ID`, `IPAM_OIDC_CLIENT_SECRET`, `IPAM_OIDC_SCOPES`, `IPAM_OIDC_REDIRECT_URI`, `IPAM_OIDC_AUTO_LOGIN`, `IPAM_OIDC_BUTTON_TEXT`, `IPAM_OIDC_ALLOWED_USERS`, `IPAM_OIDC_ALLOWED_GROUPS`, `IPAM_OIDC_GROUPS_CLAIM` | SSO |
| `IPAM_SESSION_DAYS`, `IPAM_COOKIE_SECURE` | sessions |
| `IPAM_AUTH_ENABLED=true` | legacy: password sign-in on both zones |
| `IPAM_AUTH_RESET=true` | **resets** Settings → Security to the defaults and prints a new admin password. Remove it after one start. |

Locked out? Either:
- add `IPAM_AUTH_USER: "rescue"` and `IPAM_AUTH_PASSWORD: "something-long"`, sign in
  with that and fix the settings, then remove the two variables; or
- set `IPAM_AUTH_RESET: "true"`, restart, get the new admin password from the log,
  then remove the variable.

A password hash for the environment can be made with the image itself:

```bash
docker run --rm ghcr.io/samschultzponsys/spazcat-ipam python -c \
  "from werkzeug.security import generate_password_hash as g; print(g('yourpassword'))"
```

In compose YAML, write each `$` in the hash as `$$`. Failed password and token
attempts are rate limited: 10 per 15 minutes per client IP.

### OIDC setup

1. At your provider (Authentik, Authelia, Keycloak, Pocket ID, Google, …), create an
   OAuth2/OIDC "confidential" client. Use the redirect URI shown in Settings →
   Security, which is `https://<your ipam url>/auth/oidc/callback`.
2. In Settings → Security → SSO, enter the issuer URL, client ID and secret, click
   **Test provider**, then **Save security settings**.
3. Tick *SSO (OIDC)* for WAN and/or LAN, then save again.

The flow uses PKCE and validates the ID token's signature, issuer, audience and
nonce. With auto sign-in on, `/login?manual=1` still shows the login page, and
signing out lands there too, so you don't bounce straight back into SSO.

### Behind a reverse proxy (Nginx Proxy Manager, Traefik, Caddy…)

1. Proxy `https://ipam.example.com` → `http://<docker host>:20080` (websockets not
   needed). NPM: add a Proxy Host, scheme `http`, port `20080`, then request an SSL
   certificate and turn on *Force SSL*.
2. The app trusts `X-Forwarded-For` / `-Proto` / `-Host` **only** from the
   trusted proxies (default: private ranges and loopback, which covers NPM on
   the same host or LAN). It reads the header right to left, so a client can't
   inject a fake LAN address.
3. Set the public URL in Settings → Security if the proxy doesn't send
   `X-Forwarded-Host` / `-Proto`. This matters for the OIDC redirect URI.
4. If you only ever reach the app over HTTPS, tick *HTTPS-only cookie*.
5. Open Settings → Security from outside (for example, on your phone with Wi-Fi
   off) and check that it shows your public IP and **WAN**.

Don't port-forward 20080 straight from your router. With Docker's userland proxy,
every connection can appear to come from the Docker gateway (a private address),
which would count as LAN. The Security tab warns when it sees this.

### Sessions

Sessions are signed, HttpOnly, SameSite=Lax cookies. Their length and the
HTTPS-only flag are set in Settings → Security. `IPAM_SECRET_KEY` optionally sets
the signing secret; otherwise a stable one is generated and stored in the DB.
Before sign-in, only the login page, the app name, the logo font, the font files
and `/healthz` are reachable.

## Other environment variables

| var | purpose |
|-----|---------|
| `IPAM_DB` | DB path (default `/data/ipam.db`); backups and drop-in fonts sit next to it |
| `PORT` | listen port (default `20080`) |
| `IPAM_THREADS` | web server threads (default 8) |
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
