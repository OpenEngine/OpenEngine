#!/bin/sh
# Run from the extracted release. Requires only POSIX tools and internet for CPython.
set -eu
release=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$release"
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) target=x86_64-unknown-linux-gnu ;;
  Darwin/arm64) target=aarch64-apple-darwin ;;
  *) echo 'Unsupported platform (Linux x86_64 and macOS arm64 only)' >&2; exit 1 ;;
esac
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS >/dev/null
else
  shasum -a 256 -c SHA256SUMS >/dev/null
fi
# The bundled uv must be for this platform; validate before running it.
grep -q "\"platform\": \"$target\"" manifest.json
prefix=${OPENENGINE_INSTALL_DIR:-"$HOME/.local/share/openengine/runtime"}
mkdir -p "$prefix"
prefix=$(CDPATH= cd -- "$prefix" && pwd)
if [ -e "$prefix/env" ]; then
  echo "An environment already exists at $prefix/env; choose a new OPENENGINE_INSTALL_DIR." >&2
  exit 1
fi
cp uv "$prefix/uv"
export UV_PYTHON_INSTALL_DIR="$prefix/python"
export UV_CACHE_DIR="$prefix/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_NO_CONFIG=1
"$prefix/uv" python install --no-bin 3.12.12
"$prefix/uv" venv --python 3.12.12 "$prefix/env"
"$prefix/uv" pip install --python "$prefix/env/bin/python" --no-index \
  --find-links "$release/wheels" --only-binary=:all: --require-hashes -r requirements.txt
"$prefix/uv" pip check --python "$prefix/env/bin/python"
cp manifest.json "$prefix/manifest.json"
bin=${OPENENGINE_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$bin"
ln -sf "$prefix/env/bin/engine-web" "$bin/engine-web"
printf 'Installed. Start in the foreground with: %s/engine-web\n' "$bin"
