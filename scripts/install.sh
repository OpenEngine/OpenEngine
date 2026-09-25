#!/bin/sh
# Install an OpenEngine release for the current user:
#
#   curl -LsSf https://openengine.sh/install.sh | sh
#   curl -LsSf https://openengine.sh/install.sh | sh -s -- --version 0.3.0
#
# Needs curl, tar, and a SHA-256 tool. Python comes from a pinned uv that is
# kept apart from any uv, Python, or configuration already on the machine.
# Running it again is safe: an installed version is reused, and an existing
# engine.toml or state directory is never touched.
#
# OPENENGINE_RELEASE_URL names a directory holding release-manifest.json and
# the archive (https:// or file://), in place of the GitHub release.

set -eu

REPOSITORY="OpenEngine/OpenEngine"
PYTHON_VERSION="3.12"
UV_VERSION="0.9.28"

usage() {
  cat <<EOF
Install OpenEngine for the current user.

Usage: install.sh [--version X.Y.Z] [--prefix DIR] [--no-start] [--no-browser]

  --version X.Y.Z  install this release instead of the latest
  --prefix DIR     install under DIR (default: \$XDG_DATA_HOME/openengine)
  --no-start       do not start OpenEngine after installing
  --no-browser     do not open OpenEngine in a browser
EOF
}

say() { printf 'openengine: %s\n' "$*"; }
warn() { printf 'openengine: warning: %s\n' "$*" >&2; }
die() { printf 'openengine: error: %s\n' "$*" >&2; exit 1; }

version=""
prefix=""
start=1
browser=1
while [ $# -gt 0 ]; do
  case $1 in
    --version) [ $# -ge 2 ] || die "--version needs a value"; version=$2; shift ;;
    --version=*) version=${1#*=} ;;
    --prefix) [ $# -ge 2 ] || die "--prefix needs a value"; prefix=$2; shift ;;
    --prefix=*) prefix=${1#*=} ;;
    --no-start) start=0 ;;
    --no-browser) browser=0 ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done
version=${version#v}

[ -n "${HOME:-}" ] || die "HOME is not set"
for tool in curl tar uname mkdir mktemp; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d ' ' -f 1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
else
  die "sha256sum or shasum is required"
fi

case $(uname -s) in
  Darwin) platform=apple-darwin ;;
  Linux) platform=unknown-linux-musl ;;
  *) die "unsupported operating system $(uname -s); OpenEngine installs on macOS and Linux" ;;
esac
case $(uname -m) in
  x86_64 | amd64) cpu=x86_64 ;;
  arm64 | aarch64) cpu=aarch64 ;;
  *) die "unsupported CPU $(uname -m); OpenEngine installs on x86_64 and arm64" ;;
esac
uv_target="$cpu-$platform"
case $uv_target in
  aarch64-apple-darwin) uv_sha256=12163fe09eb292d3ad1ea0f132a84485c902e2ff360d57562bf676e6615fcba0 ;;
  x86_64-apple-darwin) uv_sha256=3a8030881d13b824e5168f5e4d060e715e40753249766bda3d52d6771d93b169 ;;
  aarch64-unknown-linux-musl) uv_sha256=eec3249254efac972d2555ff858f8ed20f05b40fbb38ac83b15cf0a2ccc86749 ;;
  x86_64-unknown-linux-musl) uv_sha256=83cd032167b6b97ac94830608efe11159b3d485654e39fdb0bf84718ef236afe ;;
esac

data_home=${XDG_DATA_HOME:-$HOME/.local/share}
prefix=${prefix:-$data_home/openengine}
config_dir=${XDG_CONFIG_HOME:-$HOME/.config}/openengine
state_dir=${XDG_STATE_HOME:-$HOME/.local/state}/openengine
cache_dir=${XDG_CACHE_HOME:-$HOME/.cache}/openengine
bin_dir=${XDG_BIN_HOME:-$HOME/.local/bin}
config="$config_dir/engine.toml"
shim="$bin_dir/openengine"

mkdir -p "$prefix/bin" "$prefix/versions"
prefix=$(cd "$prefix" && pwd -P)
work=$(mktemp -d "$prefix/.install.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

fetch() {
  curl --fail --silent --show-error --location --retry 3 --output "$2" "$1" \
    || die "could not download $1"
}

verify() {
  actual=$(sha256 "$1")
  [ "$actual" = "$2" ] || die "SHA-256 mismatch for $(basename "$1"): expected $2, got $actual"
}

# Nothing from the caller's uv setup applies: no configuration files, no UV_*
# variables, no active virtualenv, and only the Python uv downloads itself.
for name in $(env | sed -n 's/^\(UV_[A-Za-z0-9_]*\)=.*/\1/p'); do
  unset "$name"
done
unset VIRTUAL_ENV CONDA_PREFIX PYTHONHOME PYTHONPATH
export UV_NO_CONFIG=1
export UV_PYTHON_INSTALL_DIR="$prefix/python"
export UV_PYTHON_PREFERENCE=only-managed
export UV_CACHE_DIR="$cache_dir/uv"

uv="$prefix/bin/uv"
case $("$uv" --version 2>/dev/null || true) in
  "uv $UV_VERSION" | "uv $UV_VERSION "*) ;;
  *)
    say "downloading uv $UV_VERSION"
    fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_target.tar.gz" "$work/uv.tar.gz"
    verify "$work/uv.tar.gz" "$uv_sha256"
    tar -xzf "$work/uv.tar.gz" -C "$work"
    mv -f "$work/uv-$uv_target/uv" "$uv"
    ;;
esac

if [ -n "${OPENENGINE_RELEASE_URL:-}" ]; then
  release_url=${OPENENGINE_RELEASE_URL%/}
elif [ -n "$version" ]; then
  release_url="https://github.com/$REPOSITORY/releases/download/v$version"
else
  release_url="https://github.com/$REPOSITORY/releases/latest/download"
fi
fetch "$release_url/release-manifest.json" "$work/release-manifest.json"
# The manifest is written by scripts/build_release.py with two-space indents;
# top-level keys are the only ones at that depth.
manifest_value() {
  sed -n "s/^  \"$1\": \"\([^\"]*\)\",\{0,1\}\$/\1/p" "$work/release-manifest.json"
}
release=$(manifest_value version)
case $release in
  '' | .* | *[!0-9A-Za-z.+-]*) die "release-manifest.json at $release_url names no valid version" ;;
esac
[ -z "$version" ] || [ "$version" = "$release" ] \
  || die "requested version $version, but the release manifest is for $release"
archive_sha256=$(manifest_value archive_sha256)
[ -n "$archive_sha256" ] || die "release $release predates this installer; choose a newer --version"

target="$prefix/versions/$release"
if [ -f "$target/.installed" ]; then
  say "OpenEngine $release is already installed"
else
  say "downloading OpenEngine $release"
  archive="$work/openengine-$release.tar.gz"
  fetch "$release_url/openengine-$release.tar.gz" "$archive"
  verify "$archive" "$archive_sha256"
  tar -xzf "$archive" -C "$work"
  [ -f "$work/openengine-$release/requirements.txt" ] || die "the archive holds no openengine-$release bundle"
  # A directory without the marker is an install that did not finish.
  rm -rf "$target"
  mv "$work/openengine-$release" "$target"
  say "installing OpenEngine $release with Python $PYTHON_VERSION"
  "$uv" venv --quiet --python "$PYTHON_VERSION" "$target/venv"
  (cd "$target" && "$uv" pip install --quiet --python "$target/venv/bin/python" \
    --require-hashes -r requirements.txt)
  : >"$target/.installed"
fi
ln -sfn "versions/$release" "$prefix/current"

# The shim: single-quoted paths, with any quote in them closed and escaped.
shell_quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
mkdir -p "$bin_dir"
if [ -e "$shim" ] && ! grep -q '^# Written by the OpenEngine installer' "$shim"; then
  die "$shim exists and was not written by this installer; move it aside and rerun"
fi
cat >"$work/shim" <<EOF
#!/bin/sh
# Written by the OpenEngine installer; rerunning it rewrites this file.
if [ -z "\${ENGINE_CONFIG:-}" ]; then ENGINE_CONFIG=$(shell_quote "$config"); fi
export ENGINE_CONFIG
exec $(shell_quote "$prefix/current/venv/bin/engine-web") "\$@"
EOF
chmod 755 "$work/shim"
mv -f "$work/shim" "$shim"

mkdir -p "$state_dir"
if [ -e "$config" ]; then
  say "keeping the existing $config"
else
  mkdir -p "$config_dir"
  toml_string() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
  OE_STATE=$(toml_string "$state_dir") OE_WORKFLOWS=$(toml_string "$prefix/current/workflows") awk '
    BEGIN { print "# Written by the OpenEngine installer, which never overwrites it.\n" }
    /^\[/ { section = $0 }
    section == "[state]" && /^directory[ \t]*=/ { print "directory = " ENVIRON["OE_STATE"]; s++; next }
    section == "[workflows]" && /^directory[ \t]*=/ { print "directory = " ENVIRON["OE_WORKFLOWS"]; w++; next }
    { print }
    END { if (s != 1 || w != 1) exit 1 }
  ' "$target/engine.toml" >"$work/engine.toml" || die "the release's default engine.toml has an unexpected layout"
  chmod 600 "$work/engine.toml"
  # -n: a config that appeared since the check above is still not replaced.
  mv -n "$work/engine.toml" "$config"
  say "wrote $config"
fi

say "installed OpenEngine $release: $shim"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) warn "$bin_dir is not on PATH; add it to your shell profile to run openengine" ;;
esac

[ "$start" = 1 ] || exit 0
port=${ENGINE_PORT:-$(sed -n 's/^port[ \t]*=[ \t]*\([0-9][0-9]*\).*/\1/p' "$config" | head -n 1)}
url="http://127.0.0.1:${port:-4364}"
if curl --fail --silent --output /dev/null "$url/api/health"; then
  say "OpenEngine is already running at $url; restart it if it predates this install"
else
  log="$state_dir/openengine.log"
  say "starting OpenEngine (log: $log)"
  nohup "$shim" </dev/null >>"$log" 2>&1 &
  server=$!
  tries=0
  until curl --fail --silent --output /dev/null "$url/api/health"; do
    if ! kill -0 "$server" 2>/dev/null || [ "$tries" -ge 60 ]; then
      tail -n 20 "$log" >&2 || true
      die "OpenEngine did not start; see $log"
    fi
    tries=$((tries + 1))
    sleep 1
  done
  say "OpenEngine is running at $url"
fi
if [ "$browser" = 1 ]; then
  case $platform in
    apple-darwin) open "$url" >/dev/null 2>&1 || true ;;
    *) if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true; fi ;;
  esac
fi
