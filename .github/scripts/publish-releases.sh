#!/usr/bin/env bash
# Create every missing GitHub Release - one per "## x.y" in CHANGELOG.md.
#   usage: publish-releases.sh <current x.y> <image>      (run by build-image.yml)
#
# - A version's tag goes on the LAST commit of main's history that shipped it,
#   i.e. the commit its final :x.y image was built from (the current version:
#   this build's commit). History before CHANGELOG.md existed counts as 1.0.
# - Missing tags are created as annotated tags dated to that commit, so old
#   versions show their real dates. GitHub refuses that push from the built-in
#   GITHUB_TOKEN when the commit has an older workflow file ("without
#   `workflows` permission"); then the Releases API creates a plain tag on the
#   same commit instead. Add a RELEASE_TOKEN secret (classic PAT with repo +
#   workflow, or fine-grained Contents + Workflows read/write) to always get
#   dated tags. GitHub doesn't allow backdating the release itself; its title
#   and notes carry the original release date.
# - Problems are reported as warnings; they never fail the image build.
# - Already-released versions are only touched if their notes no longer match
#   CHANGELOG.md (then the notes are updated), so re-running is harmless.
# - DRY_RUN=1 prints what would happen without touching anything.
set -uo pipefail
CURRENT="$1"
IMAGE="$2"
HEAD_SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
run() { if [ -n "${DRY_RUN:-}" ]; then echo "DRY: $*"; else "$@"; fi; }

top_version() {   # newest version in a commit's CHANGELOG.md (1.0 before it existed)
  git show "$1:CHANGELOG.md" 2>/dev/null | grep -m1 -oE '^## \[?v?[0-9]+\.[0-9]+' \
    | grep -oE '[0-9]+\.[0-9]+' || echo "1.0"
}

declare -A LAST
while read -r sha; do
  LAST[$(top_version "$sha")]="$sha"
done < <(git rev-list --first-parent --reverse "$HEAD_SHA")
LAST[$CURRENT]="$HEAD_SHA"

notes_for() {   # <version> <commit> -> notes file
  local v="$1" sha="$2" img_tag="$1"
  # 1.0 predates the x.y image tags - point at the image that actually exists
  [ "$v" = "1.0" ] && img_tag="sha-${sha:0:7}"
  "$HERE/release-notes.sh" "$v" "$IMAGE" | sed "s#\`$IMAGE:$v\`#\`$IMAGE:$img_tag\`#" > "notes-$v.md"
}

for v in $(grep -oE '^## \[?v?[0-9]+\.[0-9]+' CHANGELOG.md | grep -oE '[0-9]+\.[0-9]+' | sort -V -u); do
  tag="v$v"
  if [ -z "${DRY_RUN:-}" ] && gh release view "$tag" >/dev/null 2>&1; then
    notes_for "$v" "${LAST[$v]:-$HEAD_SHA}"
    if [ "$(gh release view "$tag" --json body -q .body)" != "$(cat "notes-$v.md")" ]; then
      echo "$tag: release exists - notes differ from CHANGELOG.md, updating"
      gh release edit "$tag" --notes-file "notes-$v.md" \
        || echo "::warning::Could not update the notes of $tag"
    else
      echo "$tag: release exists and is up to date"
    fi
    continue
  fi
  sha="${LAST[$v]:-}"
  if [ -z "$sha" ]; then
    echo "$tag: no commit on main ships this version - skipped"
    continue
  fi
  tag_args=(--verify-tag)
  if ! git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
    date="$(git log -1 --format=%cI "$sha")"
    run env GIT_COMMITTER_DATE="$date" git -c user.name="github-actions[bot]" \
      -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
      tag -f -a "$tag" "$sha" -m "Spazcat IPAM $tag"
    if ! run git push origin "refs/tags/$tag"; then
      echo "::notice::$tag: dated tag push refused - creating the tag through the release instead"
      git tag -d "$tag" >/dev/null 2>&1 || true
      tag_args=(--target "$sha")
    fi
  fi
  notes_for "$v" "$sha"
  date_txt="$(grep -m1 -E "^## \[?v?${v//./\\.}" CHANGELOG.md | sed -E 's/^## [^ ]+[[:space:]]*[-–—(]*[[:space:]]*//; s/[)[:space:]]*$//')"
  latest="--latest=false"; [ "$v" = "$CURRENT" ] && latest="--latest"
  echo "$tag -> ${sha:0:7} (${date_txt:-no date}) $latest"
  if ! run gh release create "$tag" "${tag_args[@]}" --title "$tag${date_txt:+ — $date_txt}" \
      --notes-file "notes-$v.md" "$latest"; then
    if [ -z "${RELEASE_TOKEN:-}" ]; then
      echo "::warning::$tag not created: GitHub doesn't let the built-in token tag a commit with an older workflow file. Add a RELEASE_TOKEN repository secret (see README) and re-run - it will be created then."
    else
      echo "::warning::Could not create release $tag (see above) - it will be retried on the next build"
    fi
    failed=1
  fi
done
[ -n "${failed:-}" ] && echo "::warning::Some releases were not created"
exit 0
