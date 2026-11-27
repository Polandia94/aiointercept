"""Pytest plugin: session-scoped fixtures for aiointercept."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

import pytest

try:
    import pytest_asyncio
except ImportError:  # pragma: no cover
    pytest_asyncio = None  # type: ignore[assignment]

from .core import aiointercept

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _fail_missing_pytest_asyncio() -> None:
    pytest.fail(
        "the aiointercept pytest fixtures need pytest-asyncio 0.24 or newer; "
        "install it with `pip install aiointercept[pytest-asyncio]`",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def aiointercept_options() -> dict[str, Any]:
    """Keyword arguments for the session-wide ``aiointercept`` server; override in ``conftest.py``."""
    return {}


if pytest_asyncio is not None and "loop_scope" in inspect.signature(pytest_asyncio.fixture).parameters:

    @pytest_asyncio.fixture(loop_scope="session", scope="session")
    async def aiointercept_server(aiointercept_options: dict[str, Any]) -> AsyncIterator[aiointercept]:
        async with aiointercept(**aiointercept_options) as m:
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

else:  # pragma: no cover  (exercised in a subprocess by the tests)

    @pytest.fixture(scope="session")
    def aiointercept_server() -> None:
        _fail_missing_pytest_asyncio()

    @pytest.fixture
    def aiointercept_mock() -> None:
        _fail_missing_pytest_asyncio()
