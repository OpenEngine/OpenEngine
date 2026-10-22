"""Build a version-locked guest bundle without Docker or model credentials."""
import argparse
from pathlib import Path
import shutil
import subprocess


def build(destination, fixture=False):
    root = Path(__file__).resolve().parents[3]
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run(['uv', 'export', '--project', str(root), '--package', 'openengine-virtual-control-plane',
                    '--locked', '--no-dev', '--no-emit-workspace', '--output-file', str(destination / 'requirements.txt')], check=True, stdout=subprocess.DEVNULL)
    for package in ('packages/persistence', 'services/virtual_control_plane'):
        subprocess.run(['uv', 'build', '--wheel', '--out-dir', str(destination), str(root / package)], check=True)
    shutil.copyfile(Path(__file__).with_name('guest.py'), destination / 'guest.py')
    if fixture:
        shutil.copyfile(root / 'services/virtual_control_plane/tests/guest_acp_fixture.py', destination / 'fixture.py')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--fixture', action='store_true', help='include deterministic ACP test adapter; no real model')
    arguments = parser.parse_args()
    build(arguments.destination, arguments.fixture)
