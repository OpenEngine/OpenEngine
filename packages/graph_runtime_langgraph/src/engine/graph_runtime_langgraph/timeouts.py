"""ACP invocation deadlines, distinct from sandbox command budgets."""

import math

DEFAULT_NODE_TIMEOUT = 1800.0


def node_timeout(value: object) -> float:
    message = "node timeout must be finite positive seconds"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)  # noqa: TRY004 -- invalid configuration uses a ValueError contract
    try:
        seconds = float(value)
    except OverflowError as error:
        raise ValueError(message) from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(message)
    return seconds
