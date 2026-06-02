"""Shared pytest fixtures and helpers."""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])

_RETRY_DELAYS = (None, 1, 2, 4, 11)


def network_retry(fn: F) -> F:
    """Retry an async test on network failure with delays of 1, 2, 4, and 11 seconds."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        for delay in _RETRY_DELAYS:
            if delay is not None:
                await asyncio.sleep(delay)
            try:
                return await fn(*args, **kwargs)
            except Exception:
                if delay == _RETRY_DELAYS[-1]:
                    raise
        raise RuntimeError("retry logic failed unexpectedly")  # This should never happen.

    return wrapper  # type: ignore[return-value]
