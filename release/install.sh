#!/bin/sh
# Offline bootstrap: only POSIX shell, tar and a system SHA-256 utility required.
set -eu
bundle=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$bundle"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c SHA256SUMS >/dev/null
else
    shasum -a 256 -c SHA256SUMS >/dev/null
fi
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64|Linux-x86_64) ;;
    *) echo 'Unsupported platform' >&2; exit 1 ;;
esac
# Separate install and application data. Refuse overwrites; upgrades are separate work.
prefix=${1:-"$HOME/.local/share/openengine-release"}
case "$prefix" in /*) ;; *) echo 'Installation path must be absolute' >&2; exit 1 ;; esac
if [ -e "$prefix" ]; then echo "Installation path already exists: $prefix" >&2; exit 1; fi
mkdir -p "$prefix"
trap 'echo "Installation failed; remove the incomplete directory: $prefix" >&2' 0
cp uv "$prefix/uv"
tar -xzf python.tar.gz -C "$prefix"
export UV_PYTHON_INSTALL_DIR="$prefix/python"
export UV_PYTHON_DOWNLOADS=never
export UV_OFFLINE=1
# The bundled uv selects the bundled interpreter without consulting system Python.
"$prefix/uv" venv --managed-python --python @PYTHON_VERSION@ "$prefix/venv"
"$prefix/venv/bin/python" "$bundle/install.py" "$bundle" "$prefix"
trap - 0
