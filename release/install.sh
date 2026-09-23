#!/bin/sh
# Offline installer. Requires only the platform's shell and checksum utility.
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$bundle"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check --quiet SHA256SUMS
else
    shasum -a 256 --check --quiet SHA256SUMS
fi
# Only execute bundled code after verifying the release contents.
identity=$(./python/bin/python3 -I -B -c 'import json,platform; m=json.load(open("manifest.json")); assert m["platform"] == platform.system().lower()+"-"+platform.machine().lower(), "unsupported platform"; print(m["version"]+"-"+m["platform"])')
prefix=${ENGINE_INSTALL_DIR:-"$HOME/.local/share/openengine/releases/$identity"}
case "$prefix" in /*) ;; *) echo 'ENGINE_INSTALL_DIR must be absolute' >&2; exit 1;; esac
if [ -e "$prefix" ]; then
    echo "Refusing to overwrite existing release: $prefix" >&2
    exit 1
fi
mkdir -p "$prefix"
cp -R "$bundle/." "$prefix/"
export UV_PYTHON_DOWNLOADS=never
export UV_OFFLINE=1
export PYTHONDONTWRITEBYTECODE=1
"$prefix/uv" venv --no-project --no-managed-python --python "$prefix/python/bin/python3" "$prefix/venv"
"$prefix/uv" pip install --python "$prefix/venv/bin/python" --no-index --no-build \
    --require-hashes --find-links "$prefix/wheels" -r "$prefix/requirements.txt"
"$prefix/uv" pip check --python "$prefix/venv/bin/python"
bin=${ENGINE_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$bin"
"$prefix/python/bin/python3" -I -B - "$prefix" "$bin" <<'PY'
import pathlib, shlex, sys
prefix, bin_dir = map(pathlib.Path, sys.argv[1:])
launcher = bin_dir / "engine-web"
if launcher.is_symlink():
    launcher.unlink()
launcher.write_text("#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexec " + shlex.quote(str(prefix / "venv/bin/engine-web")) + ' "$@"\n')
launcher.chmod(0o755)
PY
printf 'Installed %s. Start in the foreground with: %s/engine-web\n' "$identity" "$bin"
