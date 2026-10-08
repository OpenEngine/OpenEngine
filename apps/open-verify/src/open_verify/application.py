"""Discover runnable application entry points independently of the changed files."""

import json
import os
import tomllib
from pathlib import Path

from open_verify.scope import OMIT

WEB_TOOLS = {'vite', 'next', 'nuxt', 'react-scripts', 'astro', 'svelte-kit', 'webpack', 'parcel'}


def application_surface(project: Path) -> dict | None:
    """Find bounded manifest evidence for UI, CLI or API smoke coverage.

    Evidence comes from the checkout, not the diff: backend-only changes must
    still exercise an available application. Unknown/library projects remain
    eligible for explicit public-library checks instead of an invented UI.
    """
    candidates = []
    for count, (directory, dirs, files) in enumerate(os.walk(project, followlinks=False)):
        if count >= 2000:
            break
        folder = Path(directory)
        relative = folder.relative_to(project)
        dirs[:] = sorted(d for d in dirs if d not in OMIT and not d.startswith('.')
                         and not (folder / d).is_symlink() and len(relative.parts) < 4)
        for name in ('package.json', 'pyproject.toml', 'Cargo.toml'):
            path = folder / name
            if name not in files or path.is_symlink():
                continue
            try:
                if path.stat().st_size > 256_000:
                    continue
                text = path.read_text(encoding='utf-8')
                value = json.loads(text) if name == 'package.json' else tomllib.loads(text)
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(value, dict):
                continue
            evidence = path.relative_to(project).as_posix()
            if name == 'package.json':
                scripts = value.get('scripts', {})
                if isinstance(scripts, dict) and any(
                    key in {'dev', 'start', 'serve', 'preview'} and isinstance(command, str)
                    and any(tool in command.split() for tool in WEB_TOOLS)
                    for key, command in scripts.items()
                ):
                    return {'interface': 'browser', 'evidence': evidence,
                            'reason': 'Runnable web client declared in package scripts'}
                if value.get('bin'):
                    candidates.append({'interface': 'terminal', 'evidence': evidence,
                                       'reason': 'User-facing executable declared by package bin'})
            elif name == 'pyproject.toml':
                metadata = value.get('project', {})
                if isinstance(metadata, dict) and metadata.get('scripts'):
                    dependencies = metadata.get('dependencies', [])
                    api = isinstance(dependencies, list) and any(
                        isinstance(dep, str) and dep.lower().startswith(('fastapi', 'litestar'))
                        for dep in dependencies)
                    candidates.append({'interface': 'http' if api else 'terminal',
                                       'evidence': evidence,
                                       'reason': 'Runnable entry points declared by project.scripts'})
            elif value.get('bin') or (folder / 'src/main.rs').is_file():
                candidates.append({'interface': 'terminal', 'evidence': evidence,
                                   'reason': 'Runnable Rust binary declared by Cargo'})
    return candidates[0] if candidates else None


def inline_harness(argv: list[str]) -> bool:
    """Recognize interpreter snippets that cannot substitute for an app CLI action."""
    words = [Path(word).name.lower() for word in argv]
    for index, word in enumerate(words[:-1]):
        if (word.startswith(('python', 'pypy')) and words[index + 1] in {'-', '-c'}
                or word in {'node', 'nodejs', 'deno', 'bun'} and words[index + 1] in {'-e', '--eval', '-'}):
            return True
    return False
