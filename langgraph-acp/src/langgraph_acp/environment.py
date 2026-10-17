"""Explicit host inheritance for local ACP launches.

Credentials, proxy configuration and agent-specific overrides must be supplied
in the provider's env mapping. No prefix-based inheritance is performed.
"""

import os
from collections.abc import Mapping


HOST_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "SystemRoot", "WINDIR",
    "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "__CF_USER_TEXT_ENCODING", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
})


def agent_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Approved host defaults, overlaid only with explicitly granted values."""
    return {**{key: value for key, value in os.environ.items() if key in HOST_ENV_ALLOWLIST}, **(env or {})}


__all__ = ["HOST_ENV_ALLOWLIST", "agent_environment"]
