#!/usr/bin/env bash
# Print the GitHub Release body for one version, taken from CHANGELOG.md.
#   usage: release-notes.sh <x.y> [image]      e.g. release-notes.sh 1.6 ghcr.io/owner/name
# Used by build-image.yml for every new version (and to backfill old ones).
set -euo pipefail
V="$1"
IMAGE="${2:-ghcr.io/samschultzponsys/spazcat-ipam}"
CHANGELOG="${CHANGELOG:-CHANGELOG.md}"
RE="^## \\[?v?${V//./\\.}\\]?([^0-9]|$)"

# the "## x.y — date" heading's date part, then everything up to the next "## "
DATE=$( (grep -m1 -E "$RE" "$CHANGELOG" || true) | sed -E 's/^## \[?v?[0-9]+\.[0-9]+\]?[[:space:]]*[-–—(]*[[:space:]]*//; s/[)[:space:]]*$//')
BODY=$(awk -v re="$RE" '$0 ~ re {f=1; next} f && /^## / {exit} f {print}' "$CHANGELOG" \
       | sed -e '/./,$!d' | sed -e ':a' -e '/^\n*$/{$d;N;ba' -e '}')

if [ -z "$BODY" ]; then
  echo "No CHANGELOG.md section for $V" >&2
  exit 1
fi

printf '%s\n\n---\n' "$BODY"
[ -n "$DATE" ] && printf '**Released:** %s  \n' "$DATE"
printf '**Docker image:** `%s:%s`\n\n' "$IMAGE" "$V"
printf '```bash\ndocker compose pull && docker compose up -d\n```\n'
