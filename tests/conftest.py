"""Shared pytest fixtures and helpers."""

import asyncio
import functools
import gzip
import json
import ssl
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
_RETRYABLE = (aiohttp.ClientError, TimeoutError, OSError)


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


def _raw_lines(request: web.Request, name: bytes) -> list[str]:
    """Each separate header line named ``name``, in order (not re-split like ``getall``)."""
    return [v.decode() for k, v in request.raw_headers if k.lower() == name]


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

    async def two_cookies(request: web.Request) -> web.Response:
        resp = web.Response(text="ok")
        resp.headers.add("Set-Cookie", "a=1; Path=/")
        resp.headers.add("Set-Cookie", "b=2; Path=/")
        resp.headers.add("Set-Cookie", "c=3; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/")
        return resp

    async def echo_dup(request: web.Request) -> web.Response:
        return web.json_response(_raw_lines(request, b"x-dup"))

    async def echo_cookie_lines(request: web.Request) -> web.Response:
        return web.json_response(_raw_lines(request, b"cookie"))

    async def two_links(request: web.Request) -> web.Response:
        resp = web.Response(text="ok")
        resp.headers.add("Link", '<https://a.test/>; rel="next"')
        resp.headers.add("Link", '<https://b.test/>; rel="prev"')
        return resp

    app = web.Application()
    app.router.add_route("*", "/status/{code}", status)
    app.router.add_get("/gzip", gzipped)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/cookies", cookies)
    app.router.add_get("/two-cookies", two_cookies)
    app.router.add_get("/echo-dup", echo_dup)
    app.router.add_get("/echo-cookie-lines", echo_cookie_lines)
    app.router.add_get("/two-links", two_links)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture
async def tls_upstream() -> AsyncIterator[tuple[TestServer, ssl.SSLContext]]:
    """A local HTTPS server (private trustme CA, cert for ``localhost``) plus a client context trusting that CA.

    Reach it as ``https://localhost:{port}/...`` so requests go through DNS interception.
    """
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("localhost").configure_cert(server_ctx)
    client_ctx = ssl.create_default_context()
    ca.configure_trust(client_ctx)

    async def hello(request: web.Request) -> web.Response:
        return web.Response(text="secure upstream")

    app = web.Application()
    app.router.add_route("*", "/hello", hello)
    server = TestServer(app)
    await server.start_server(ssl=server_ctx)
    yield server, client_ctx
    await server.close()
