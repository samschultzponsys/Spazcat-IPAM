# CLAUDE.md — Spazcat IPAM

Self-hosted IP address manager layered on top of DHCP controllers. It pulls
clients from a controller (UniFi, Omada, OPNsense, Pi-hole, AdGuard Home, …),
lets you sort them into color-coded pools, and flags duplicate IPs. It ships
as one Docker image: `ghcr.io/samschultzponsys/spazcat-ipam` (amd64 and arm64),
port **20080**. Open items are in `NOTES.md`.

## Stack and layout
- **Backend:** Python 3.12 (image) with Flask, served by **Waitress** as a single process
  (`IPAM_THREADS`, default 8). SQLite in WAL mode (`/data/ipam.db`).
- **Frontend:** a single `index.html` using React 18 and Babel standalone from unpkg.
  **There is no build step, no bundler and no npm.**
- `app/app.py`: the app, DB init and migrations, settings, pools, sync engine
  (`perform_sync`, `match_pool`, `sort_devices`), changelog and version, fonts,
  update check, CSV export, the auto-sync thread and `/healthz`.
- `app/auth.py`: sign-in. It handles the LAN/WAN zones, the methods none, local,
  oidc and token, the session gate (`before_request`), users, tokens, OIDC through
  Authlib with PKCE, and rate limiting.
- `app/branding.py`: logo and icon styles, SVG and Pillow PNG icon rendering, uploads.
- `app/platforms.py`: one `Platform` subclass per controller. Each `FIELDS` key
  doubles as a settings key, and secret fields are write-only.
- `app/static/`: `index.html` (the whole UI), `login.html`, `brand.js` (shared
  logo/icon code), `fonts/` (fonts baked into the image).
- `.github/workflows/build-image.yml`: builds and pushes the image, then publishes
  GitHub Releases.
- `.github/scripts/`: `publish-releases.sh` and `release-notes.sh`.
- `compose.yaml`: the only supported deploy. It uses the prebuilt image.

## Run / test / deploy
- **Deploy:** `docker compose pull && docker compose up -d`. Data lives in `./data`:
  `ipam.db`, `backups/`, `branding/` and `fonts/`. The admin password is printed in
  a box in `docker logs spazcat-ipam` until it's changed.
- **Do not reintroduce** manual or build-it-yourself compose options. The user
  wants prebuilt images only.
- **Local run:** use a venv. Debian's system `cryptography` breaks `authlib`, so
  don't use system pip packages.
  ```
  python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt
  IPAM_DB=/tmp/ipam.db PORT=20080 .venv/bin/python app/app.py
  ```
- **No automated test suite.** Changes are verified by:
  - fake controllers or a fake IdP written as tiny Flask apps
  - `curl` against the API
  - Playwright with Chromium at `/opt/pw-browsers`. In the sandbox unpkg is
    blocked, so route `unpkg.com` requests to locally `npm pack`ed React/Babel files.
  - Dismiss the changelog popup first; it auto-opens and blocks clicks.
- Locally, Python is 3.11, but the image runs 3.12. Avoid 3.12-only syntax, such
  as backslashes inside f-string expressions.
- Check mobile at ≤640px. The breakpoint is the `@media (max-width:640px)` block
  plus `useIsMobile`.

## Versioning and releases
- **The newest `## x.y — YYYY-MM-DD` heading in CHANGELOG.md is the version.** The
  app reads it at runtime, and CI uses it as the image tag. Bump it nowhere else.
- The minor version is always a single digit: 1.0 … 1.9, then **2.0**. CI fails
  any heading that doesn't match `^[0-9]+\.[0-9]$`.
- Each push to main builds the tags `latest`, `x.y` and `sha-<short>`, then runs
  `publish-releases.sh`, which:
  - creates or updates one GitHub Release `vx.y` per changelog version, tagged on
    the last commit that shipped it, with that version's section as the notes
  - never fails the build
  - can be tried locally with `DRY_RUN=1`
- **Per user-facing change:**
  1. Add or extend the changelog entry. Write it in plain language, as Added /
     Changed / Fixed / Security sections.
  2. **Update `readme.md` in the same commit.** The user expects the README to
     always be current.
- Docs-only changes don't bump the version.

## Conventions
- **Branches:** the user approved pushing straight to `main`. Also keep the session
  branch (e.g. `claude/gracious-franklin-ojogx4`) in sync with main.
- **Commits:** end each message with the `Co-Authored-By` and `Claude-Session`
  trailers. Never put model names or IDs in commits or files.
- **DB migrations are additive only:** `ALTER TABLE ADD COLUMN`, guarded by a
  column check in `init_db`. The app version change triggers an automatic backup
  to `/data/backups`, which keeps the newest 10.
- **Settings:** stored in the `settings` key/value table. Structured values (auth
  config, logo/icon style, pool style) are JSON strings, cleaned by a `clean_*`
  function before they're saved.
- **MACs:** always store them normalized via `normalize_mac` → `aa:bb:cc:dd:ee:ff`.
- **UI:**
  - every dialog has the `CloseX` × button
  - inputs are 16px on mobile to stop iOS zoom
  - touch devices get no drag and drop; tap a row to edit it
  - colors come from `PALETTE` (40 presets)
- **Writing style for docs and changelogs:** short, plain, user-facing. Lowercase
  `readme.md` is intentional.

## Gotchas and non-obvious decisions
- **Auth config:**
  - It lives in the DB (`auth_config`). `IPAM_AUTH_*` and `IPAM_OIDC_*` env vars
    override it *and lock that field in the UI*; they're meant for recovery.
  - `IPAM_AUTH_RESET=true` restores the defaults (local sign-in, user `admin`, new
    password).
  - **Only sessions signed in with local or oidc can edit Security.** On a zone
    with no sign-in the user has to "elevate".
  - A save that would lock the editor out returns **409** and needs confirming.
- **Client IP:** `X-Forwarded-For` is honored only from `trusted_proxies` and read
  right to left. Otherwise a WAN client could spoof a LAN address.
- **OIDC:**
  - auto-redirect is decided **server-side**, not in browser storage
  - `/login?manual=1` is the escape hatch
  - not yet tested against a real IdP
- **Duplicate IPs** count only *active claims*: online, reserved, locked or manual.
  Old rows get a `STALE` tag instead.
- **UniFi:**
  - "reserved" means `use_fixedip` is true; `fixed_ip` alone means nothing
  - reservations must clear when the controller drops them
  - `last_seen` comes from the controller, never from the sync time
- **The auto-sync thread must start exactly once.** Waitress is single-process; keep
  it that way, or add a lock before scaling out.
- **Fonts:**
  - files in `app/static/fonts` or `/data/fonts` are served through generated
    `/api/fonts.css` as `ipamf-<slug>`
  - the logo defaults to a non-italic "ethnocentric" file
  - **never bind-mount over `/app/static/fonts`.** That hid the baked-in fonts for
    the user once.
- **Caching:** the user runs Nginx Proxy Manager with asset caching.
  - Pages, the API, `fonts.css` and the manifest are sent as no-cache.
  - Icons are cache-busted with `?v=<icon_version>`; bump it on any icon change.
- **Uploads:** raster uploads are re-encoded to PNG. SVGs are regex-checked for
  scripts and served with a strict CSP.
- **Update check:**
  - Reads GHCR tags. Anonymous works for public images; a **classic** PAT with
    `read:packages` is needed for private ones.
  - **GHCR rejects fine-grained PATs** (`github_pat_…`), so with those the check
    reads the repo's `CHANGELOG.md` via the GitHub API instead.
  - The token is only ever sent to `ghcr.io` and `api.github.com`.
  - The user's token is fine-grained.
- **Releases:**
  - **Claude Code web sessions can't push tags or create releases** (the proxy
    returns 403); let the Action do it.
  - The Action's `GITHUB_TOKEN` can't create tags on commits whose workflow file
    differs, which blocks the backfill. That needs the `RELEASE_TOKEN` secret (see
    NOTES.md).
- **awk:** `awk -v re=…` processes backslash escapes under gawk, so
  `release-notes.sh` uses a plain string compare. Keep it portable across gawk
  and mawk.
- **Sync rules:**
  - grace fallback moves offline, non-reserved devices to Unallocated after the
    grace period
  - prune (off by default) skips locked, reserved, manual and noted devices
  - auto-pool picks the most specific CIDR and skips stale devices
  - locked devices never move
