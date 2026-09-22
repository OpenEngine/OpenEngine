#!/bin/sh
# Run from the extracted, platform-specific release. No host Python is used.
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64|Linux-x86_64) ;;
  *) echo 'Supported platforms: macOS arm64, Linux x86_64' >&2; exit 1 ;;
esac
(cd "$bundle" && if command -v sha256sum >/dev/null; then
  sha256sum -c SHA256SUMS
else
  shasum -a 256 -c SHA256SUMS
fi)
# All bootstrap files, including uv and the manifest, were checked above.
# This location also owns the managed Python installation and download cache.
case "$(uname -s)" in
  Darwin) base="$HOME/Library/Application Support/OpenEngine" ;;
  Linux) base="${XDG_DATA_HOME:-$HOME/.local/share}/OpenEngine" ;;
esac
export UV_PYTHON_INSTALL_DIR="$base/python"
export UV_CACHE_DIR="$base/cache"
"$bundle/uv" python install --managed-python 3.14.7
"$bundle/uv" run --no-project --no-config --managed-python --python 3.14.7 \
  "$bundle/install.py" "$bundle" "$base"
