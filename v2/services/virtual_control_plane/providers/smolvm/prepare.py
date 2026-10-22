"""Executed inside the VM, with an idempotent completion marker."""
import json
from pathlib import Path
import shutil
import subprocess
import sys


def prepare(specification, runtime_id):
    marker = Path('/oe-vcp-ready')
    if marker.exists() and marker.read_text() == runtime_id:
        return
    root = Path('/workspace')
    root.mkdir(exist_ok=True)
    for tool in specification.get('tools', []):
        if shutil.which('sh' if tool == 'terminal' else tool) is None:
            message = f'Required tool missing from image: {tool}'
            raise RuntimeError(message)
    context = specification.get('context', {})
    for index, repository in enumerate(context.get('repositories', [])):
        destination = root / 'repositories' / str(index)
        destination.parent.mkdir(exist_ok=True)
        if not (destination / '.git').is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            subprocess.run(['git', 'clone', '--', repository['url'], str(destination)], check=True)
        if repository.get('ref'):
            subprocess.run(['git', '-C', str(destination), 'checkout', '--detach', repository['ref'], '--'], check=True)
    files = list(context.get('files', []))
    if context.get('instructions'):
        files.append({'path': 'AGENTS.md', 'content': context['instructions']})
    for file in files:
        target = root / file['path']
        if not target.resolve().is_relative_to(root.resolve()):
            message = 'Context path escapes workspace'
            raise ValueError(message)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file['content'])
    marker.write_text(runtime_id)


if __name__ == '__main__':
    prepare(json.loads(sys.argv[1]), sys.argv[2])
