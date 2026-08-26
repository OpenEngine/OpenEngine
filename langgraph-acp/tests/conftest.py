"""What the test modules here share, in the one place pytest guarantees them.

`asyncio_test` was written out twice, in `test_client.py` and `test_store.py`,
because there was nowhere else for it to live. There is now: pytest puts this
directory on `sys.path` before it collects anything in it, so a test module
imports the decorator by name and the package's async story has a single
spelling to change.
"""

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest

# `store_conformance` is asserted in, not collected, so pytest would leave its
# asserts unrewritten and a failure would say `assert False`. Naming it here --
# before anything imports it -- is what makes it say which store and which
# method instead.
pytest.register_assert_rewrite("store_conformance")


def asyncio_test(
    test: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., None]:
    """Run an async test. A decorator rather than a plugin, so that the package
    keeps the empty dependency list its first ticket established."""

    @wraps(test)
    def synchronously(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return synchronously
