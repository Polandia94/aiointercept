"""Shared pytest fixtures and helpers."""

import asyncio
import functools
import gzip
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, TypeVar

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])

_RETRY_DELAYS = (None, 1, 2, 4, 11)
# Retry only transport-level failures: an AssertionError (or any other test
# bug) must fail immediately instead of burning ~18s of retries on it.
_RETRYABLE = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


def network_retry(fn: F) -> F:
    """Retry an async test on network failure with delays of 1, 2, 4, and 11 seconds."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        for delay in _RETRY_DELAYS:
            if delay is not None:
                await asyncio.sleep(delay)
            try:
                return await fn(*args, **kwargs)
            except _RETRYABLE:
                if delay == _RETRY_DELAYS[-1]:
                    raise
        raise RuntimeError("retry logic failed unexpectedly")  # This should never happen.

    return wrapper  # type: ignore[return-value]


GZIP_PAYLOAD: dict[str, Any] = {"gzipped": True, "padding": "x" * 2048}


@pytest.fixture
async def real_upstream() -> AsyncIterator[TestServer]:
    """A local aiohttp server standing in for "the real internet".

    Passthrough tests target it via ``http://localhost:{port}/...``, which the
    real resolver resolves normally — exercising the same passthrough code
    paths as an external host, without depending on the network.
    """

    async def status(request: web.Request) -> web.Response:
        return web.Response(status=int(request.match_info["code"]), text="upstream")

    async def gzipped(request: web.Request) -> web.Response:
        body = gzip.compress(json.dumps(GZIP_PAYLOAD).encode())
        # Explicit Content-Encoding plus the auto Content-Length of the
        # *compressed* body: the exact response shape that used to truncate
        # proxied bodies.
        return web.Response(body=body, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})

    async def redirect(request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": str(request.url.with_path("/status/200"))})

    async def cookies(request: web.Request) -> web.Response:
        resp = web.json_response({"cookie_header": request.headers.get("Cookie")})
        resp.set_cookie("upstream-session", "sekrit")
        return resp

    app = web.Application()
    app.router.add_route("*", "/status/{code}", status)
    app.router.add_get("/gzip", gzipped)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/cookies", cookies)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()
