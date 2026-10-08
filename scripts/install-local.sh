#!/bin/sh
# Build OpenEngine from this checkout and install it the way the one-line
# installer does, replacing whichever version `engine` runs now:
#
#   scripts/install-local.sh
#   scripts/install-local.sh --no-start
#
# Options go to scripts/install.sh. The install is labeled with the package
# version and the checked-out commit, such as 1.2.0-ffdb7cd. A tree with
# uncommitted changes gets a -dirty.<timestamp> suffix, so each build of it
# installs afresh; earlier dirty builds are removed once the new one is in.
# Rerun the one-line installer to return to a release.
#
# Needs git, uv, and npm in addition to what install.sh needs.

set -eu

die() { printf 'openengine: error: %s\n' "$*" >&2; exit 1; }

for tool in git uv npm; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$root"

commit=$(git rev-parse HEAD)
version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || die "pyproject.toml names no version"
label="$version-$(git rev-parse --short=7 HEAD)"
dirty=0
if [ -n "$(git status --porcelain)" ]; then
  label="$label-dirty.$(date -u +%Y%m%d%H%M%S)"
  dirty=1
fi

# install.sh's --prefix, to find the dirty builds this one supersedes.
prefix=${XDG_DATA_HOME:-$HOME/.local/share}/openengine
next=""
for arg in "$@"; do
  case $next$arg in
    --prefix) next=--prefix= ;;
    --prefix=*) prefix=${arg#--prefix=}; next="" ;;
    *) next="" ;;
  esac
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

# Reinstall web dependencies whenever the lockfile differs from the one they
# were installed from, so a checkout never builds against stale packages.
lock_stamp=apps/web/node_modules/.install-local-package-lock.json
if ! cmp -s apps/web/package-lock.json "$lock_stamp"; then
  npm --prefix apps/web ci
  cp apps/web/package-lock.json "$lock_stamp"
fi
printf 'openengine: building OpenEngine %s\n' "$label"
uv run --no-project python scripts/build_release.py \
  --commit "$commit" --label "$label" --output "$work/release"

status=0
OPENENGINE_RELEASE_URL="file://$work/release" sh scripts/install.sh "$@" || status=$?
if [ "$status" = 0 ] && [ "$dirty" = 1 ]; then
  for old in "$prefix"/versions/*-dirty.*; do
    if [ -d "$old" ] && [ "${old##*/}" != "$label" ]; then rm -rf "$old"; fi
  done
fi
exit "$status"
