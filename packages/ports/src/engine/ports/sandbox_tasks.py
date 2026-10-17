"""Ownership rules shared by sandbox adapters and their runtime wrappers.

Never abandon an owned task on caller cancellation. Worker failure takes
precedence over deferred cancellation, independent of when it completes. After
success, callers record acquired resources before propagating cancellation.
"""
import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

_P = ParamSpec('_P')
_T = TypeVar('_T')


async def settle_task(task: asyncio.Task[_T]) -> tuple[_T, bool]:
    """Return the worker result and whether the caller was cancelled.

    Repeated caller cancellation never cancels the worker. A failed or cancelled
    worker raises its own error after settling; successful resource acquisitions
    must be registered by the caller before honoring the returned flag.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 -- retrieve the settled worker's original failure below
            break
    return task.result(), cancelled


async def filesystem_work(function: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Settle a thread before releasing its files; failure wins cancellation."""
    result, cancelled = await settle_task(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))
    if cancelled:
        raise asyncio.CancelledError
    return result
