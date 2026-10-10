# Notes — open items

## Blocked on the user
- **Backfill GitHub Releases v1.0–v1.5.**
  - Add a `RELEASE_TOKEN` repo secret. Either works:
    - a fine-grained PAT on this repo with Contents and Workflows set to read & write
    - a classic PAT with the `repo` and `workflow` scopes
  - Then run Actions → build-image → Run workflow. `publish-releases.sh` will tag
    each old version on its original commit and fill in the notes.
  - v1.6 is already published.

## Unverified / needs real hardware
- Omada reservation field names (`ipSetting`, `fixedIp`) are a best guess.
- Every platform except UniFi has been tested only against fake controllers.
  Alta Labs Route10 (SSH) is marked experimental.
- OIDC has been tested only against a fake IdP. Try Authentik, Pocket ID or Keycloak.
- The phone layout has been checked only in Playwright's mobile viewport, not on
  real iOS or Android devices.

## Maintenance
- GitHub Actions warns that Node 20 actions are deprecated. Bump
  `actions/checkout@v4` and the `docker/*` actions when new majors are out.
