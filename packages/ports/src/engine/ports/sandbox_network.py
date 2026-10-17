"""Exact DNS hostname grants shared by graph configuration and sandbox backends."""

from collections.abc import Sequence
import ipaddress
import re

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def allowed_hosts(value: Sequence[str]) -> tuple[str, ...]:
    """Normalize exact DNS names; refuse URLs, ports, wildcards and IP ranges."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        message = "allowed_hosts must be a list or tuple of exact DNS hostnames"
        raise ValueError(message)  # noqa: TRY004 -- configuration values share a ValueError contract
    result: list[str] = []
    for host in value:
        if not isinstance(host, str) or not host or len(host) > 253:
            message = "allowed_hosts entries must be exact DNS hostnames"
            raise ValueError(message)
        normalized = host.lower()
        if not all(_HOST.fullmatch(label) for label in normalized.split(".")):
            message = "allowed_hosts entries must be exact DNS hostnames without URLs, ports or wildcards"
            raise ValueError(message)
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            message = "allowed_hosts entries must be DNS hostnames, not IP addresses"
            raise ValueError(message)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)
