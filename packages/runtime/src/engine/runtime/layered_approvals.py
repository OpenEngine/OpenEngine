"""Resolve immutable run defaults beneath an operator-owned approval ceiling."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
import tomllib

from engine.runtime.approval_policy import _matches
from engine.runtime.config import ApprovalConfig, EngineConfigError, parse_engine_config


REPOSITORY_SECTIONS = ("approvals", "approvals.bash")


class PolicyError(ValueError):
    """A policy is malformed or a run attempts to loosen its defaults."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def policy_values(policy: ApprovalConfig) -> dict[str, Any]:
    values = asdict(policy)
    values.pop("repositories")
    values["allow"] = [str(item) for item in policy.allow]
    values["bash"] = {key: list(value) for key, value in values["bash"].items()}
    return values


def parse_policy(values: Mapping[str, Any]) -> ApprovalConfig:
    return parse_engine_config({"approvals": dict(values)}).approvals


def _flatten(values: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        message = "approvals must be an object"
        raise PolicyError(message)
    if unknown := set(values) - {"auto_approve", "allow", "bash"}:
        message = f"unknown approval setting: {sorted(unknown)[0]}"
        raise PolicyError(message)
    # Use the configuration parser for identical validation in TOML and JSON.
    try:
        parse_policy(values)
    except EngineConfigError as error:
        raise PolicyError(str(error)) from error
    return {
        **{key: value for key, value in values.items() if key != "bash"},
        **{f"bash.{key}": value for key, value in values.get("bash", {}).items()},
    }


def parse_overrides(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        message = "approvals must be an object"
        raise PolicyError(message)
    _flatten(value)
    return value


def _nested(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "auto_approve": values["auto_approve"], "allow": values["allow"],
        "bash": {key: values[f"bash.{key}"] for key in ("allow", "ask", "deny")},
    }


def _covered(pattern: str, ceiling: list[str]) -> bool:
    # Arbitrary glob inclusion cannot be established by globbing one pattern
    # against another. Accept identical patterns, universal patterns, or a
    # literal command that matches a ceiling pattern; fail closed otherwise.
    return any(
        parent in ("*", "**") or pattern == parent
        or (parent.endswith("*") and not any(char in parent.rstrip("*") for char in "*?[")
            and pattern.startswith(parent.rstrip("*")))
        or (not any(char in pattern for char in "*?[") and _matches(pattern, parent))
        for parent in ceiling
    )


def resolve_policy(
    backend: ApprovalConfig,
    repository: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    *,
    repository_warnings: tuple[str, ...] = (),
    repository_allowed: bool = True,
    trusted: bool = False,
    reject_instead_of_ask: bool = False,
) -> dict[str, Any]:
    """One resolution path for previews, runs and unattended callers.

    Missing values inherit. Allow lists intersect; deny/ask lists accumulate.
    A repo is clamped with warnings; the same attempted relaxation by a run is
    an error. Spend and PR limits deliberately have no repository/run key here.
    """
    if reject_instead_of_ask:
        if overrides and overrides.get("auto_approve") is True:
            message = "unattended runs cannot request auto_approve"
            raise PolicyError(message)
        overrides = {**(overrides or {}), "auto_approve": False}
    current = _flatten(policy_values(backend))
    provenance = dict.fromkeys(current, "backend")
    warnings: list[str] = list(repository_warnings)
    if trusted:
        warnings.append("[trusted_repos] is deprecated; use approvals.repositories (the backend ceiling still applies)")
        repository_allowed = True
    if repository is not None and not repository_allowed:
        warnings.append("repository approvals ignored: backend approvals.repositories does not delegate to this repository")
        repository = None
    for layer, values in (("repository", repository), ("run", overrides)):
        if values is None:
            continue
        supplied = _flatten(values)
        for key, wanted in supplied.items():
            prior = current[key]
            if key == "auto_approve":
                resolved = bool(prior and wanted)
            elif key in ("bash.deny", "bash.ask"):
                resolved = list(dict.fromkeys([*prior, *wanted]))
                if layer == "run" and not set(prior).issubset(wanted):
                    message = f"run approvals.{key} cannot remove inherited restrictions"
                    raise PolicyError(message)
            elif key == "bash.allow":
                resolved = [pattern for pattern in wanted if _covered(pattern, prior)]
            else:
                resolved = [capability for capability in wanted if capability in prior]
            if key not in ("bash.deny", "bash.ask") and resolved != wanted:
                message = f"{layer} approvals.{key} exceeds the effective policy"
                if layer == "run":
                    raise PolicyError(message + "; runs may only tighten approvals")
                warnings.append(message + "; clamped to backend ceiling")
                provenance[key] = "repository ∩ backend"
            else:
                provenance[key] = layer if resolved == wanted else f"{provenance[key]} ∩ {layer}"
            current[key] = resolved
        # An explicit capability restriction must also switch off blanket
        # consent; otherwise auto_approve would make a narrower allow inert.
        if "allow" in supplied or "bash.allow" in supplied:
            if supplied.get("auto_approve") is True and layer == "run":
                message = "run auto_approve cannot accompany an explicit allow restriction"
                raise PolicyError(message)
            if supplied.get("auto_approve") is True:
                warnings.append("repository auto_approve disabled by explicit allow restrictions")
            current["auto_approve"] = False
            provenance["auto_approve"] = layer
    return {
        **_nested(current), "provenance": provenance, "warnings": warnings,
        "reject_instead_of_ask": reject_instead_of_ask,
    }


async def read_repository_policy(repository: str, ref: str = "HEAD", *, defer_missing_ref: bool = False) -> tuple[Mapping[str, Any] | None, str, tuple[str, ...]]:
    """Read a committed blob at a pinned base revision, never a working file."""
    root = Path(repository).expanduser()
    if not root.is_dir():
        return None, "", ()
    root = root.resolve()
    root = next((parent for parent in (root, *root.parents) if (parent / ".git").exists()), root)
    if not any((parent / ".git").exists() for parent in (root, *root.parents)):
        return None, "", ()

    async def git(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            message = f"cannot read repository approval policy at {ref}: {stderr.decode().strip()}"
            raise PolicyError(message)
        return stdout.decode()

    try:
        revision = (await git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")).strip()
    except PolicyError:
        if defer_missing_ref:
            # Provisioning can fetch a remote ref not present in the source
            # checkout. It will resolve policy at that actual base before agents.
            return None, "", ()
        raise
    entry = await git("ls-tree", revision, "--", "engine.toml")
    if not entry:
        return None, revision, ()
    if not entry.startswith("100644 blob ") and not entry.startswith("100755 blob "):
        message = "engine.toml must be a regular committed file"
        raise PolicyError(message)
    try:
        document = tomllib.loads(await git("show", f"{revision}:engine.toml"))
    except tomllib.TOMLDecodeError as error:
        message = f"invalid engine.toml: {error}"
        raise PolicyError(message) from error
    selected: dict[str, Any] = {}
    ignored: list[str] = []

    def select_sections(source: Mapping[str, Any], target: dict[str, Any], prefix: str = "") -> None:
        for key, value in source.items():
            section = f"{prefix}.{key}" if prefix else key
            if section in REPOSITORY_SECTIONS:
                if isinstance(value, Mapping):
                    target[key] = {}
                    select_sections(value, target[key], section)
                else:
                    target[key] = value  # Keep malformed opted-in sections for validation.
            elif prefix in REPOSITORY_SECTIONS and not isinstance(value, Mapping):
                target[key] = value
            else:
                ignored.append(section)

    select_sections(document, selected)
    warnings = (
        (f"repository engine.toml ignored sections/settings: {', '.join(sorted(ignored))}",)
        if ignored else ()
    )
    values = selected.get("approvals")
    if values is not None:
        _flatten(values)
    return values, revision, warnings
