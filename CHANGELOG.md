# Changelog

Versions are always `MAJOR.MINOR` with a single-digit minor: `1.0`, `1.1` … `1.9`, then `2.0`.
The newest `## x.y` heading below is the version the app reports and the tag the
GitHub Action publishes the image under — bump it here and nowhere else.

## 1.5 — 2026-09-24

### Added
- **Logo style** (Settings → Appearance), previewed live on the real logo:
  - **Gradient** with 2–3 colors, a direction and 10 presets (Spazcat, Sunset,
    Ocean, Aurora, Fire, Candy, Mint, Neon, Gold, Steel)
  - **Solid** single color
  - **Outline** with a color and width
  - an optional **pattern overlay** on gradient or solid (stripes, diagonal, dots,
    checker, grid) with its own color, strength and scale
  - an optional **glow**

  The login page uses the same style.
- **Icon** for the browser tab and the home screen:
  - **Recolor the stock icon:** gradient or solid background, direction, tile color,
    and a rounded, circle or square shape. **Match logo** copies your logo colors.
  - **Upload your own:** PNG, JPG, WebP, GIF, ICO or SVG, up to 2 MB. Raster images
    are cleaned and re-encoded to PNG, and the home-screen icons are generated from
    it. SVGs are checked for scripts and used for the browser tab only.
- Icon URLs carry a version number, so browsers and reverse-proxy caches (such as
  Nginx Proxy Manager's "Cache Assets") pick up a new icon straight away.

## 1.4 — 2026-09-24

### Added
- **Phone layout.** On screens up to 640px wide:
  - a compact toolbar with a **⋯ menu** for CSV export, theme, text size and sign out
  - device rows that stack (IP, badges and lock, then name, then MAC) so nothing
    runs off the edge
  - pool headers that wrap, with notes on their own line
  - slightly smaller text overall (A−/A+ still adjust it)
- **Tap a device to open it** on phones. Touch screens can't drag and drop, so use
  the dialog's Pool field to move a device.
- **Delete button** in the device dialog.
- **× close button** on every dialog (changelog, settings, pool, device). It stays in
  the corner while you scroll.
- **Add to Home Screen:** a web app manifest and proper icons, so the app opens full
  screen like a native app, using your app name.

### Changed
- On phones, dialogs open as sheets from the bottom and use one column. The
  settings tabs scroll sideways, and the security table stacks.
- Form fields use 16px text on phones, so iPhones no longer zoom in when you tap one.
- Pages and API responses are sent with no-cache headers, so a browser or proxy
  can't keep showing an old version after you update.

## 1.3 — 2026-09-24

### Added
- **Sign-in is configured in the app.** Settings → Security now lets you:
  - pick the methods for LAN and WAN
  - add and remove users and change passwords
  - create and revoke access tokens (each shown once, with a ready-made link)
  - set up SSO (OIDC), including a **Test provider** button, the redirect URI to copy,
    the button text and auto sign-in
  - set networks, trusted proxies and session length
- **Secure by default.** New installs require sign-in on LAN and WAN as user
  `admin`, with a random password printed in a box in the container log
  (`docker logs spazcat-ipam`). The box reappears on every start until you change
  the password, and a banner in the app reminds you too.
- **Environment variables override the UI.** Every `IPAM_AUTH_*` / `IPAM_OIDC_*`
  variable still works; a field it sets shows as locked in the UI. Use them to
  recover from a broken config, or set `IPAM_AUTH_RESET=true` once to restore the
  defaults with a new admin password.
- **HTTP Basic auth** with a password user, for scripts.
- **40 preset pool colors** (was 11), plus a hex color field.

### Changed
- **Upgrading from 1.2 without `IPAM_AUTH_*` variables turns sign-in on.** Get the
  admin password from `docker logs spazcat-ipam`, then change it, or allow
  "No sign-in" on your LAN in Settings → Security.
- Only someone signed in with a password or SSO can change security settings. On a
  network with no sign-in, the Security tab has a "Sign in to edit" link. Saves that
  would lock you out are refused or need confirming.
- Access tokens are stored hashed.

## 1.2 — 2026-09-24

### Added
- **Pool settings.** Each pool's edit dialog now styles its header with a live
  preview. You can set:
  - the font (any installed font, or the global header font), size, bold, italic,
    UPPERCASE and letter spacing
  - the name color and the header tint
  - whether the color dot, subnets, notes and device count show
- **Hide when empty**, per pool. An empty hidden pool comes back while you drag a
  device, so it still works as a drop target. It also shows whenever it has devices.
  That includes Unallocated, which reappears the moment anything lands in it.
  A "N empty pools hidden — show" link lets you reach hidden pools to edit them.
- **Collapse caret** on every pool header. It's remembered per browser, and
  collapsed pools still accept drops.
- **Sign-in methods for reverse proxies (Nginx Proxy Manager, Traefik, Caddy…).**
  - Choose `none`, `local` (username/password), `oidc` (SSO) or `token` (access
    link / API bearer token), separately for **LAN** and **WAN** connections.
  - Methods can be combined, and any one enabled method is enough to sign in.
    `none` can't be combined with others.
  - The real client IP is read from `X-Forwarded-For`, but only from trusted
    proxies, so a WAN client can't pretend to be on your LAN.
- **OIDC**
  - Works with Authentik, Authelia, Keycloak, Pocket ID, Google and others.
  - Uses PKCE and can be limited to specific users or groups.
  - Optional **auto sign-in** that sends visitors straight to your identity provider.
  - The login page button text is customizable.
- **Access links:** `https://ipam.example.com/?token=…` signs a browser in and
  removes the token from the address bar. `Authorization: Bearer …` works for scripts.
- **Settings → Security** (read-only) shows the IP and zone the app sees for you,
  whether you came through a proxy, and the active sign-in rules. It warns about
  risky setups.
- Failed password and token attempts are rate limited (10 per 15 minutes per client).
- `/healthz` endpoint and a Docker health check.

### Changed
- Sign-in is configured with environment variables (`IPAM_AUTH_LAN`,
  `IPAM_AUTH_WAN`, …), so nobody using the app can switch it off.
  `IPAM_AUTH_ENABLED=true` still works and means `local` on both LAN and WAN.
- The app now runs on the Waitress production web server instead of Flask's
  development server. The port is unchanged (20080).
- The session cookie was renamed (`spazcat_ipam_session`), so you'll sign in once
  after upgrading.
- The database uses SQLite WAL mode, so syncs and page loads don't block each other.
- The manual / build-it-yourself deploy option was removed. Deploy with the
  published container image only.

## 1.1 — 2026-09-23

### Added
- **More sync platforms.** A dropdown in Settings → Platform chooses the source: UniFi,
  TP-Link Omada (Open API), Alta Labs Route10 (SSH, experimental), OpenWrt (SSH),
  MikroTik RouterOS 7, OPNsense, Pi-hole v6, AdGuard Home, Technitium DNS, or
  None (manual only).
- **Auto-sync in minutes, hours or days.**
- **Automatic pool sorting.** New devices land directly in the pool whose subnet
  contains their IP (most specific subnet wins). Optionally re-sort every device on
  every sync, manual or timed. Locked devices never move.
- **"Sort into pools now" button** (Settings → Sync & pools) for devices that existed
  before a pool was defined. Creating or editing a pool can also pull in its matching
  devices straight away.
- **Custom fonts.** Commit `.otf` / `.ttf` / `.woff` / `.woff2` files to `app/static/fonts/`
  to bake them into the image, or drop them into `data/fonts/` without a rebuild. Pick
  separate fonts for the logo, the headers and the body text in Settings → Appearance.
- **Custom app name** for the wordmark in the top left, the browser tab and the login page.
- **Version tag and changelog.** Click the version in the bottom-left corner to read this
  changelog. It opens by itself the first time you visit and again after an update.
  A pulsing dot appears when a newer image tag is published.
- **Optional pruning** of offline clients not seen for N days (off by default).
- **Automatic database backup** to `data/backups/` before the app upgrades to a new
  version. The newest 10 are kept.

### Fixed
- **Phantom duplicate IPs.** Only live claims now count as duplicates: devices that are
  online, reserved, locked or added manually. An offline client that merely *used* to
  hold an address gets a muted `STALE` tag instead of setting off the banner.
- UniFi clients with a fixed IP that was later switched off were still treated as
  reserved on their old address.
- A reservation removed on the controller never cleared in the IPAM.
- Every client the controller remembered was stamped "seen now" on each sync. This
  hid how old it was and stopped the grace-period fallback from ever running.
- MAC addresses are stored in one format (`aa:bb:cc:dd:ee:ff`), so `AA-BB-…` from another
  controller can't create a second row for the same device.
- Editing a device's MAC in the edit dialog now saves.

### Changed
- Offline, non-reserved devices past the grace period now actually fall back to
  Unallocated as documented. Expect a batch of old history to move there on the first
  sync after upgrading. Lock anything you want kept in place.

## 1.0 — 2026-07-01

- Initial release: UniFi sync (API key), color-coded pools, drag-and-drop, lock,
  grace-period fallback, duplicate-IP banner, exclusions, search, CSV export,
  light/dark theme, optional login.
