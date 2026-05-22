"""Pytest plugin: session-scoped fixtures for aiointercept."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

try:
    import pytest_asyncio
except ImportError:  # pragma: no cover
    pytest_asyncio = None  # type: ignore[assignment]

from .core import aiointercept

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

if pytest_asyncio is not None:

    @pytest_asyncio.fixture(loop_scope="session", scope="session")
    async def aiointercept_server() -> AsyncIterator[aiointercept]:
        async with aiointercept() as m:
            yield m

    @pytest_asyncio.fixture
    async def aiointercept_mock(aiointercept_server: aiointercept) -> AsyncIterator[aiointercept]:
        # Re-pin _caller_loop so async callbacks dispatch on the test's loop, not the
        # (dormant) session loop the server was started on.
        aiointercept_server._caller_loop = asyncio.get_running_loop()
        try:
            yield aiointercept_server
        finally:
            aiointercept_server.clear()
