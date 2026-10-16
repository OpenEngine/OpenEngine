"""Pure launch policy: validate and compose immutable sandbox specifications.

Path resolution, credential lookup, bridge file reads and session ownership
belong to the provider. These helpers never acquire resources or write files.
"""
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from langgraph_acp import ACPConnectionError
from engine.ports import SandboxSpec
from engine.ports.sandbox_home import SandboxCredentialState, SandboxHome
from engine.ports.sandbox_sessions import SandboxSessions
from engine.ports.sandbox_socket import SandboxSocket
from engine.runtime.model_credentials import ModelCredential, ModelCredentialError, ModelProvider


# Provider identities, not registry aliases. Extra hosts require node grants.
PROVIDER_ALLOWED_HOSTS: Mapping[str, tuple[str, ...]] = {
    'codex': ('api.openai.com', 'auth.openai.com', 'chatgpt.com'),
    'claude': ('api.anthropic.com', 'console.anthropic.com', 'claude.ai', 'platform.claude.com'),
}
_MANAGED_ENV = frozenset({
    'HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'OPENAI_API_KEY', 'CODEX_API_KEY',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'DEFAULT_AUTH_REQUEST',
})


def prepare_launch_spec(spec: SandboxSpec, *, agent: str, workspace: Path,
                        env: Mapping[str, str], labels: Mapping[str, str],
                        provider: ModelProvider | None) -> SandboxSpec:
    """Paths must already be canonical; inputs remain unchanged."""
    if spec.workspace is not None and spec.workspace != workspace:
        message = 'sandbox spec workspace differs from the node workspace'
        raise ACPConnectionError(message, agent=agent, operation='connect')
    if spec.home is not None or spec.sessions is not None:
        message = 'ACP private homes are provisioned only from the selected model credential'
        raise ModelCredentialError(message)
    secrets = {**env, **spec.secrets}
    for key in secrets:
        if key.startswith(('GIT_', 'GH_', 'GITHUB_', 'GITLAB_', 'GLAB_', 'GCM_')) or key in {
            'GL_TOKEN', 'CI_JOB_TOKEN', 'CI_REPOSITORY_URL', 'SSH_AUTH_SOCK', 'SSH_ASKPASS',
        }:
            message = 'forge credentials and credential helpers cannot be injected into an ACP sandbox'
            raise ModelCredentialError(message)
    other = {'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN'} if provider == 'codex' else {'OPENAI_API_KEY', 'CODEX_API_KEY'} if provider == 'claude' else set()
    if other & secrets.keys():
        message = 'an ACP sandbox may receive only its selected model provider credential'
        raise ModelCredentialError(message)
    hosts = tuple(dict.fromkeys((*PROVIDER_ALLOWED_HOSTS.get(provider or '', ()), *(spec.egress_allowlist or ()))))
    return replace(spec, workspace=workspace, secrets=secrets, egress_allowlist=hosts,
                   labels={**spec.labels, **labels})


def with_credential(spec: SandboxSpec, credential: ModelCredential | None,
                    state: SandboxCredentialState | None) -> SandboxSpec:
    if credential is None:
        return spec
    return replace(spec, home=replace(credential.home(), credentials=state),
                   secrets={key: value for key, value in spec.secrets.items() if key not in _MANAGED_ENV})


def with_bridges(spec: SandboxSpec, profiles: Sequence[tuple[SandboxHome, SandboxSocket]]) -> SandboxSpec:
    if not profiles:
        return spec
    home = spec.home or SandboxHome(files={})
    files, redactions = dict(home.files), list(home.redactions)
    for profile, _ in profiles:
        files.update(profile.files)
        redactions.extend(profile.redactions)
    return replace(spec, home=replace(home, files=files, redactions=tuple(redactions)),
                   sockets=(*spec.sockets, *(socket for _, socket in profiles)))


def with_sessions(spec: SandboxSpec, checkpoint: SandboxSessions) -> SandboxSpec:
    home = spec.home or SandboxHome(files={})
    variable, directory = ('CODEX_HOME', '.codex') if checkpoint.provider == 'codex' else ('CLAUDE_CONFIG_DIR', '.claude')
    return replace(spec, home=replace(home, paths={**home.paths, variable: directory}), sessions=checkpoint)
