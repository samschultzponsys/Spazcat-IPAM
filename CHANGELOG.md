# Changelog

Versions are always `MAJOR.MINOR` with a single-digit minor: `1.0`, `1.1` … `1.9`, then `2.0`.
The newest `## x.y` heading below is the version the app reports and the tag the
GitHub Action publishes the image under — bump it here and nowhere else.

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
