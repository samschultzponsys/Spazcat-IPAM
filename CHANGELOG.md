# Changelog

Versions are always `MAJOR.MINOR` with a single-digit minor: `1.0`, `1.1` … `1.9`, then `2.0`.
The newest `## x.y` heading below is the version the app reports and the tag the
GitHub Action publishes the image under — bump it here and nowhere else.

## 1.1 — 2026-09-23

### Added
- **More sync platforms.** A dropdown in Settings → Platform chooses the source: UniFi,
  TP-Link Omada (Open API), Alta Labs Route10 (SSH, experimental), OpenWrt (SSH),
  MikroTik RouterOS 7, OPNsense, Pi-hole v6, or None (manual only).
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
