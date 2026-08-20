#!/usr/bin/env bash
# Build one self-contained OpenEngine release archive for the host platform.
#
# The archive is the boundary every installer will consume, so what goes in it
# is decided here and nowhere else: a standalone CPython, the application and
# its dependencies installed into that CPython, the built web client as package
# data, a launcher, and a file saying which build this is.
#
# Three properties are the point, and each is bought by a specific choice:
#
#   relocatable  the launcher resolves its own directory and runs the bundled
#                interpreter by relative path, so no absolute path from this
#                machine survives into the archive;
#   reproducible third-party dependencies are installed from `uv.lock` rather
#                than re-resolved, and the interpreter version is pinned here;
#   ours         workspace distributions are installed from locally built
#                wheels with `--no-deps`, so a name that also exists on a
#                public index cannot be what a user ends up running.
#
# Usage: packaging/build-archive.sh
#
#   OPENENGINE_VERSION         version for the archive name (default: git describe)
#   OPENENGINE_PYTHON_VERSION  standalone CPython to bundle
#   OPENENGINE_OUT_DIR         where to write the archive (default: dist/)

set -euo pipefail

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

# Pinned: the interpreter is part of the product, so which one it is has to be
# a commit rather than whatever was current on build day.
python_version="${OPENENGINE_PYTHON_VERSION:-3.13.11}"

commit=$(git -C "$repository_root" rev-parse HEAD 2>/dev/null || echo unknown)
version="${OPENENGINE_VERSION:-$(git -C "$repository_root" describe --tags --always 2>/dev/null || echo 0.0.0)}"
version="${version#v}"

out_dir="${OPENENGINE_OUT_DIR:-$repository_root/dist}"

fail() {
  echo "build-archive: $*" >&2
  exit 1
}

case "$(uname -s)" in
  Darwin) operating_system=macos ;;
  Linux) operating_system=linux ;;
  *) fail "unsupported operating system $(uname -s)" ;;
esac

case "$(uname -m)" in
  arm64 | aarch64) architecture=arm64 ;;
  x86_64 | amd64) architecture=x86_64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac

platform="$operating_system-$architecture"
name="openengine-$version-$platform"

command -v uv > /dev/null || fail "uv is required to build a release archive"
command -v npm > /dev/null || fail "npm is required to build the web client"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
root="$staging/$name"
mkdir -p "$root"

echo "==> building $name (python $python_version, commit ${commit:0:12})"

# 1. The web client, into the Python package that serves it.
echo "==> building the web client"
npm --prefix "$repository_root/apps/web" ci
npm --prefix "$repository_root/apps/web" run build
client="$repository_root/apps/web/src/engine/apps/web/client"
[ -f "$client/index.html" ] || fail "the web client did not build into $client"

# 2. A standalone CPython. `--no-bin` keeps the install to this staging tree
#    instead of also putting a `python` on the builder's PATH.
echo "==> fetching standalone CPython $python_version"
downloads="$staging/python-downloads"
uv python install --install-dir "$downloads" --no-bin "$python_version"
interpreter_root=$(find "$downloads" -mindepth 1 -maxdepth 1 -type d -name 'cpython-*' | head -n 1)
[ -n "$interpreter_root" ] || fail "no standalone CPython was installed into $downloads"
mv "$interpreter_root" "$root/python"
python="$root/python/bin/python3"
[ -x "$python" ] || fail "the bundled interpreter is missing at $python"

# 3. Third-party dependencies, at the versions `uv.lock` already resolved.
#
#    `--break-system-packages` is what installing into a standalone
#    distribution takes: it ships an `EXTERNALLY-MANAGED` marker meaning "do
#    not pip into me". Building the product *is* the sanctioned modification,
#    and the marker is left in place afterwards so the guard still stands for
#    whoever ends up with the archive.
echo "==> installing dependencies from the lockfile"
requirements="$staging/requirements.txt"
uv export \
  --directory "$repository_root" \
  --frozen \
  --no-dev \
  --package engine-web \
  --no-emit-workspace \
  --no-hashes \
  --format requirements.txt \
  --output-file "$requirements"
uv pip install \
  --python "$python" \
  --break-system-packages \
  --no-cache \
  --requirement "$requirements"

# 4. The workspace itself, from wheels built here. `--no-deps` because step 3
#    already installed everything these need: nothing about OpenEngine should
#    be fetched by name from an index.
echo "==> installing OpenEngine"
wheels="$staging/wheels"
uv build --directory "$repository_root" --all-packages --wheel --out-dir "$wheels"
uv pip install \
  --python "$python" \
  --break-system-packages \
  --no-cache \
  --no-deps \
  "$wheels"/*.whl

site_packages=$(
  "$python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
)
[ -f "$site_packages/engine/apps/web/client/index.html" ] \
  || fail "the installed application does not carry the web client"

# 5. Drop the console scripts uv generated. Each carries an absolute shebang
#    naming the interpreter by the path it had while being built, which stops
#    being a path the moment the archive is extracted somewhere else -- so they
#    would ship dead. `bin/openengine` is this archive's entrypoint and finds
#    its interpreter relatively instead. The distribution's own scripts (`pip`,
#    `pydoc`) already use a relocatable shebang and are left alone.
for script in "$root/python/bin"/*; do
  [ -f "$script" ] || continue
  case "$(head -n 1 "$script" 2> /dev/null)" in
    "#!$root/"*)
      echo "    dropping $(basename "$script"): shebang cannot be relocated"
      rm -f "$script"
      ;;
  esac
done

# 6. Who this build is. Written into the installed tree rather than the source
#    tree: a checkout has nothing to declare, and a stale file claiming it does
#    is worse than no file at all.
cat > "$site_packages/engine/apps/web/build.json" <<JSON
{
  "version": "$version",
  "commit": "$commit",
  "platform": "$platform",
  "python": "$python_version"
}
JSON
cp "$site_packages/engine/apps/web/build.json" "$root/RELEASE.json"

# 7. The launcher. Resolves symlinks first, because the installers this archive
#    exists for will put a link on PATH pointing into a versioned directory.
mkdir -p "$root/bin"
cat > "$root/bin/openengine" <<'LAUNCHER'
#!/bin/sh
# Run OpenEngine from its own bundled interpreter, wherever this tree lives.
set -eu

program=$0
while [ -L "$program" ]; do
  link=$(readlink "$program")
  case $link in
    /*) program=$link ;;
    *) program=$(dirname "$program")/$link ;;
  esac
done

root=$(CDPATH= cd -- "$(dirname -- "$program")/.." && pwd -P)
exec "$root/python/bin/python3" -m engine.apps.web.cli "$@"
LAUNCHER
chmod +x "$root/bin/openengine"

# 8. Prove the thing runs before claiming it is an archive.
echo "==> checking the staged tree"
"$root/bin/openengine" --version

mkdir -p "$out_dir"
archive="$out_dir/$name.tar.gz"
tar -czf "$archive" -C "$staging" "$name"

# `sha256sum -c` and `shasum -a 256 -c` both read this, from `$out_dir`.
(
  cd "$out_dir"
  if command -v sha256sum > /dev/null; then
    sha256sum "$name.tar.gz" > "$name.tar.gz.sha256"
  else
    shasum -a 256 "$name.tar.gz" > "$name.tar.gz.sha256"
  fi
)

echo "==> $archive"
cat "$out_dir/$name.tar.gz.sha256"
