"""Tests for aiointercept.core targeting ~100% coverage."""

import asyncio
import concurrent.futures
import logging
import re
import socket
import threading
from random import uniform
from unittest import mock

import aiohttp
import pytest
from aiohttp import ClientSession
from aiohttp.abc import AbstractResolver
from aiohttp.client_exceptions import ClientConnectionError
from aiohttp.connector import TCPConnector
from aiohttp.test_utils import TestServer
from yarl import URL

from aiointercept import CallbackResult, MockResponse, aiointercept
from tests.conftest import GZIP_PAYLOAD, network_retry


def upstream_base(server: TestServer) -> str:
    """Base URL of the local upstream using ``localhost`` so requests go
    through real DNS resolution (and its cache), like an external host."""
    return f"http://localhost:{server.port}"


# ---------------------------------------------------------------------------
# Basic mock_external_urls=True (DNS patched) vs False (direct to server)
# ---------------------------------------------------------------------------


async def test_mock_external_urls_true_basic():
    """DNS is patched so requests to example.com are intercepted and served by the mock."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/hello", status=200, body=b"hi")
        resp = await session.get("http://example.com/hello")
        assert resp.status == 200
        assert await resp.read() == b"hi"


async def test_mock_external_urls_false_uses_server_host_port():
    """Without DNS patching we connect directly to server_host:server_port."""
    async with aiointercept() as m:
        m.add(f"{m.server_url}/api", method="GET", status=201, body=b"direct")
        url = f"http://{m.server_host}:{m.server_port}/api"
        async with ClientSession() as session:
            resp = await session.get(url)
        assert resp.status == 201
        assert await resp.read() == b"direct"


async def test_requests_outside_manager():
    """check requests outside the context manager works"""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/hello", status=200, body=b"hi")
        await session.get("http://example.com/hello")

    m.assert_called_once_with("http://example.com/hello", method="GET")


async def test_explicit_start_stop():
    """start()/stop() drive the lifecycle without the context manager."""
    m = aiointercept()
    await m.start()
    try:
        assert m.server is not None
        m.get(f"{m.server_url}/ping", status=200, body=b"pong")
        async with ClientSession() as session:
            resp = await session.get(f"{m.server_url}/ping")
            assert resp.status == 200
            assert await resp.read() == b"pong"
        m.assert_called_once_with(f"{m.server_url}/ping", method="GET")
    finally:
        await m.stop()

    # stop() tears the server down and clears registered handlers.
    assert m.server is None
    assert m.handlers == {}


# ---------------------------------------------------------------------------
# URL type variants: str, URL, Pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_input",
    [
        "http://api.test/items",
        URL("http://api.test/items"),
    ],
)
async def test_add_string_and_url_object(url_input: str | URL):
    """Both plain strings and yarl URL objects are accepted as mock targets."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.add(url_input, method="GET", status=200, body=b"ok")
        resp = await session.get("http://api.test/items")
        assert resp.status == 200


async def test_add_pattern():
    """A compiled regex pattern matches any URL that satisfies the expression."""
    pattern = re.compile(r"^http://api\.test/items/\d+$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.add(pattern, method="GET", status=200, body=b"item")
        resp = await session.get("http://api.test/items/42")
        assert resp.status == 200
        assert await resp.read() == b"item"


async def test_pattern_no_match_raises():
    """A URL that does not satisfy the registered pattern raises ClientConnectionError."""
    pattern = re.compile(r"^http://api\.test/items/\d+$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.add(pattern, method="GET", status=200)
        with pytest.raises(ClientConnectionError):
            await session.get("http://api.test/items/abc")


# ---------------------------------------------------------------------------
# HTTP method shortcuts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "shortcut"),
    [
        ("GET", "get"),
        ("POST", "post"),
        ("PUT", "put"),
        ("PATCH", "patch"),
        ("DELETE", "delete"),
        ("OPTIONS", "options"),
    ],
)
async def test_method_shortcuts(method, shortcut):
    """Each HTTP verb has a convenience shortcut method on the mock object."""
    url = "http://shortcuts.test/path"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        getattr(m, shortcut)(url, status=200)
        resp = await session.request(method, url)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Response body variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_body"),
    [
        ({"body": b"bytes"}, b"bytes"),
        ({"body": "string"}, b"string"),
        ({"json": {"a": 1}}, b'{"a": 1}'),
        ({"payload": {"x": 2}}, b'{"x": 2}'),
    ],
)
async def test_response_body_variants(kwargs, expected_body):
    """body (bytes or str), json, and payload are all serialised to the expected bytes."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://body.test/", **kwargs)
        resp = await session.get("http://body.test/")
        assert await resp.read() == expected_body


# ---------------------------------------------------------------------------
# repeat parameter
# ---------------------------------------------------------------------------


async def test_repeat_true_infinite():
    """repeat=True keeps the handler registered indefinitely for any number of calls."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://repeat.test/", status=200, repeat=True)
        for _ in range(5):
            resp = await session.get("http://repeat.test/")
            assert resp.status == 200


async def test_repeat_false_once():
    """repeat=False (default) consumes the handler after a single call; subsequent calls raise."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://repeat.test/once", status=200, repeat=False)
        resp = await session.get("http://repeat.test/once")
        assert resp.status == 200
        with pytest.raises(ClientConnectionError):
            await session.get("http://repeat.test/once")


@pytest.mark.parametrize("n", [2, 3])
async def test_repeat_integer(n):
    """repeat=N allows exactly N calls before the handler is exhausted."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://repeat.test/n", status=200, repeat=n)
        for _ in range(n):
            resp = await session.get("http://repeat.test/n")
            assert resp.status == 200
        with pytest.raises(ClientConnectionError):
            await session.get("http://repeat.test/n")


async def test_repeat_zero():
    """repeat=0 is treated identically to repeat=False (respond exactly once)."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://repeat.test/once", status=200, repeat=0)
        resp = await session.get("http://repeat.test/once")
        assert resp.status == 200
        with pytest.raises(ClientConnectionError):
            await session.get("http://repeat.test/once")


async def test_consumed_handler_diagnosis(caplog):
    """Re-requesting an exhausted finite-repeat handler reports it was already consumed."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post("http://repeat.test/once", status=200, repeat=1)
        await session.post("http://repeat.test/once")
        with caplog.at_level(logging.WARNING), pytest.raises(ClientConnectionError):
            await session.post("http://repeat.test/once")
    assert caplog.messages == [
        "aiointercept: no handler for POST http://repeat.test/once — "
        "all registered handlers for this method+url have already been consumed "
        "(finite repeat exhausted)"
    ]


# ---------------------------------------------------------------------------
# Pattern repeat
# ---------------------------------------------------------------------------


async def test_pattern_repeat_true():
    """A pattern handler with repeat=True serves every matching URL without expiring."""
    pattern = re.compile(r"^http://pat\.test/.*$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.add(pattern, method="GET", status=200, repeat=True)
        for i in range(3):
            resp = await session.get(f"http://pat.test/path{i}")
            assert resp.status == 200


async def test_pattern_repeat_integer():
    """A pattern handler with repeat=2 is consumed after exactly two matching requests."""
    pattern = re.compile(r"^http://pat\.test/path$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.add(pattern, method="GET", status=200, repeat=2)
        resp = await session.get("http://pat.test/path")
        assert resp.status == 200
        resp = await session.get("http://pat.test/path")
        assert resp.status == 200


async def test_pattern_repeat_error_mixing_repeat_true_and_list():
    """if you add a repeat int after repeat True, repeat int should raise"""
    pattern = re.compile(r"^http://mix\.test/.*$")
    async with aiointercept(mock_external_urls=True) as m:
        m.add(pattern, method="GET", status=200, repeat=True)
        with pytest.raises(ValueError, match="repeat=True, cannot add more handlers"):
            m.add(pattern, method="GET", status=201, repeat=1)


async def test_url_repeat_error_mixing_repeat_true_and_list():
    """Adding a finite repeat after an infinite repeat=True on the same URL raises ValueError."""
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://mix.test/path", status=200, repeat=True)
        with pytest.raises(ValueError, match="repeat=True, cannot add more handlers"):
            m.get("http://mix.test/path", status=201, repeat=1)


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


async def test_sync_callback():
    """A synchronous callback function is invoked and its CallbackResult is returned."""

    def cb(url, **kwargs):
        return CallbackResult(status=202, body=b"cb-sync")

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://cb.test/", callback=cb)
        resp = await session.get("http://cb.test/")
        assert resp.status == 202
        assert await resp.read() == b"cb-sync"


async def test_async_callback():
    """An async callback coroutine is awaited and its CallbackResult is returned."""

    async def cb(url, **kwargs):
        return CallbackResult(status=203, body=b"cb-async")

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://cb.test/async", callback=cb)
        resp = await session.get("http://cb.test/async")
        assert resp.status == 203


async def test_callback_with_payload():
    """A callback that returns a payload dict is JSON-serialised in the response body."""

    def cb(url, **kwargs):
        return CallbackResult(status=200, payload={"key": "val"})

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://cb.test/payload", callback=cb)
        resp = await session.get("http://cb.test/payload")
        data = await resp.json()
        assert data == {"key": "val"}


# ---------------------------------------------------------------------------
# Passthrough (mock_external_urls=True with specific hosts allowed through)
# ---------------------------------------------------------------------------


async def test_passthrough_host_is_allowed(real_upstream: TestServer):
    """Passthrough host resolves normally and reaches the (local) real server."""
    upstream_url = f"{upstream_base(real_upstream)}/status/200"
    async with (
        ClientSession() as session,
        aiointercept(mock_external_urls=True, passthrough=[upstream_url]) as m,
    ):
        m.get("http://example.com/", status=200)
        mocked = await session.get("http://example.com/")
        assert mocked.status == 200
        real = await session.get(upstream_url)
        assert real.status == 200


# ---------------------------------------------------------------------------
# Stale DNS cache: a connector that resolved a host for real before the mock
# started must not reuse that resolution once mocking is active.
# ---------------------------------------------------------------------------


async def test_real_request_then_mock_same_host_ignores_stale_dns(real_upstream: TestServer):
    """A real request leaves a live keep-alive connection (and DNS cache entry)
    for the host; once the mock is active, a request to the same host is served
    by the mock, not reused against the real server."""
    url = f"{upstream_base(real_upstream)}/status/200"
    async with ClientSession() as session:
        # Real request: resolves localhost, caches its address, and leaves a
        # keep-alive connection to the real server in the pool.
        real = await session.get(url)
        assert real.status == 200

        async with aiointercept(mock_external_urls=True) as m:
            m.get(url, status=418)
            mocked = await session.get(url)
            assert mocked.status == 418
            m.assert_called_once()


async def test_registered_host_missing_path_raises():
    """Registered host with an unregistered path raises ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/registered", status=200)
        with pytest.raises(ClientConnectionError):
            await session.get("http://example.com/not-registered")


# ---------------------------------------------------------------------------
# passthrough_unmatched
# ---------------------------------------------------------------------------


async def test_passthrough_unmatched_allows_real_requests(real_upstream: TestServer):
    """Requests without a registered handler pass through to the network."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.get("http://example.com/mocked", status=200, body=b"mocked")
        mocked = await session.get("http://example.com/mocked")
        assert mocked.status == 200
        real = await session.get(f"{upstream_base(real_upstream)}/status/201")
        assert real.status == 201


async def test_passthrough_unmatched_false_raises_for_unknown():
    """With passthrough_unmatched=False, any unregistered host raises ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=False) as m:
        m.get("http://example.com/", status=200)
        with pytest.raises(ClientConnectionError):
            await session.get("http://unregistered.test/foo")


async def test_passthrough_unmatched_with_pattern_proxies_unmatched(real_upstream: TestServer):
    """
    With a pattern registered, DNS redirects ALL hosts to the test server.
    Unmatched requests fall into _dispatch's passthrough branch, whose bypass
    connector uses the original (unpatched) resolution instead of looping back
    to the test server.
    """
    pattern = re.compile(r"^http://pat\.test/specific$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.add(pattern, method="GET", status=200, repeat=True)
        # Matched: mock response
        resp_mocked = await session.get("http://pat.test/specific")
        assert resp_mocked.status == 200
        # Unmatched: proxied via real DNS to the actual server
        resp_real = await session.get(f"{upstream_base(real_upstream)}/status/201")
        assert resp_real.status == 201


# ---------------------------------------------------------------------------
# Failure scenarios
# ---------------------------------------------------------------------------


async def test_no_handler_returns_connection_error():
    """A request to a path with no registered handler raises ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as _m:
        _m.get("http://example.com/", status=200)
        with pytest.raises(ClientConnectionError):
            await session.get("http://example.com/missing")


async def test_exception_parameter_causes_connection_error():
    """exception= registers a handler that closes the connection, raising ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/err", exception=Exception("boom"), status=200)
        with pytest.raises(ClientConnectionError):
            await session.get("http://example.com/err")


async def test_method_mismatch_raises():
    """A POST to a GET-only handler raises ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/meth", status=200)
        with pytest.raises(ClientConnectionError):
            await session.post("http://example.com/meth")


async def test_unmatched_logs_method_mismatch_hint(caplog):
    """When only the method differs, the warning names the registered method."""
    caplog.set_level("WARNING", logger="aiointercept.core")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/meth", status=200)
        with pytest.raises(ClientConnectionError):
            await session.post("http://example.com/meth")
    messages = {r.message for r in caplog.records if r.name == "aiointercept.core"}
    assert len(messages) == 1, messages
    assert messages.pop() == (
        "aiointercept: no matching handler — closest registered (diff):\n"
        "- POST http://example.com/meth\n"
        "? ^^^\n"
        "+ GET http://example.com/meth\n"
        "? ^^"
    )


async def test_unmatched_logs_closest_url_hint(caplog):
    """When only the path differs, the warning names the closest registered URL."""
    caplog.set_level("WARNING", logger="aiointercept.core")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/users", status=200)
        with pytest.raises(ClientConnectionError):
            await session.get("http://example.com/userss")
    messages = {r.message for r in caplog.records if r.name == "aiointercept.core"}
    assert len(messages) == 1, messages
    (actual,) = messages
    assert actual == (
        "aiointercept: no matching handler — closest registered (diff):\n"
        "- GET http://example.com/userss\n"
        "?                             -\n"
        "+ GET http://example.com/users"
    )


async def test_add_without_server_raises():
    """Calling add() before entering the context manager (no server) raises RuntimeError."""
    m = aiointercept(mock_external_urls=True)
    with pytest.raises(RuntimeError, match="Server not started"):
        m.add("http://example.com/", method="GET", status=200)


async def test_add_url_without_host_raises():
    """A URL with no extractable host raises ValueError."""
    async with aiointercept() as m:
        with pytest.raises(ValueError, match="Cannot extract host"):
            m.add("/relative/path", status=200)


# ---------------------------------------------------------------------------
# Assert helpers
# ---------------------------------------------------------------------------


async def test_assert_called_and_not_called():
    """assert_called() and assert_not_called() reflect whether any request was made."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/assert", status=200)
        m.assert_not_called()
        await session.get("http://example.com/assert")
        m.assert_called()
        with pytest.raises(AssertionError):
            m.assert_not_called()


async def test_assert_called_once():
    """assert_called_once() passes after exactly one request and fails after a second."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/once", status=200, repeat=True)
        await session.get("http://example.com/once")
        m.assert_called_once()
        await session.get("http://example.com/once")
        with pytest.raises(AssertionError):
            m.assert_called_once()


async def test_assert_called_never_raises():
    """assert_called() raises AssertionError when no requests have been made."""
    async with aiointercept(mock_external_urls=True) as m:
        with pytest.raises(AssertionError):
            m.assert_called()


async def test_assert_any_call():
    """assert_any_call() passes for a URL that was requested and fails for one that was not."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/any", status=200)
        await session.get("http://example.com/any")
        m.assert_any_call("http://example.com/any")
        with pytest.raises(AssertionError):
            m.assert_any_call("http://example.com/other")


async def test_assert_called_with_json():
    """assert_called_with() matches the JSON body sent in the request."""
    url = "http://example.com/json"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, json={"x": 1})
        m.assert_called_with(url, method="POST", json={"x": 1})
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with(url, method="POST", json={"x": 2})
    assert str(exc_info.value) == (
        "JSON body mismatch: expected {'x': 2}, got {'x': 1}\n- {'x': 2}\n?       ^\n+ {'x': 1}\n?       ^"
    )


async def test_assert_called_with_data_bytes():
    """assert_called_with() matches raw bytes sent as the request body."""
    url = "http://example.com/data"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, data=b"rawbytes")
        m.assert_called_with(url, method="POST", data=b"rawbytes")
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with(url, method="POST", data=b"otherbytes")
    assert str(exc_info.value) == ("Body mismatch: expected otherbytes, got rawbytes\n- otherbytes\n+ rawbytes")


async def test_assert_called_with_huge_body_diff_is_bounded():
    """A mismatch between very large bodies stays fast and the message bounded.

    A raw body is a single very long line, which would otherwise push
    ``difflib.ndiff`` into quadratic blow-up; the diff body must be clipped.
    """
    url = "http://example.com/huge"
    sent = ("a" * 5000).encode()
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, data=sent)
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with(url, method="POST", data=("b" * 5000).encode())
    message = str(exc_info.value)
    # Summary line is capped, and the per-line clip marker keeps the body short.
    assert message.startswith("Body mismatch: expected ")
    assert "…(line truncated)" in message
    assert len(message) < 2000


async def test_assert_called_with_data_string():
    """assert_called_with() matches a plain string sent as the request body."""
    url = "http://example.com/strdata"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, data="hello")
        m.assert_called_with(url, method="POST", data="hello")


async def test_assert_called_with_data_dict():
    """assert_called_with() matches a form-encoded dict sent as the request body."""
    url = "http://example.com/formdata"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, data={"field": "value"})
        m.assert_called_with(url, method="POST", data={"field": "value"})
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with(url, method="POST", data={"field": "other"})
    assert str(exc_info.value) == (
        "Form-encoded body mismatch: expected {'field': ['other']}, got {'field': ['value']}\n"
        "- {'field': ['other']}\n"
        "?             ^^^ -\n"
        "+ {'field': ['value']}\n"
        "?             ^^^^"
    )


async def test_assert_called_with_headers():
    """assert_called_with() matches specific request headers and fails on wrong values."""
    url = "http://example.com/headers"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url, headers={"X-Custom": "yes"})
        m.assert_called_with(url, headers={"X-Custom": "yes"})
        with pytest.raises(AssertionError):
            m.assert_called_with(url, headers={"X-Custom": "no"})


async def test_assert_called_once_with():
    """assert_called_once_with() passes when the URL was called exactly once."""
    url = "http://example.com/oncewith"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url)
        m.assert_called_once_with(url)


async def test_assert_called_with_wrong_url():
    """assert_called_with() raises AssertionError when the URL does not match what was called."""
    url = "http://example.com/x"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url)
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with("http://example.com/y")
    assert str(exc_info.value) == (
        "No calls to GET http://example.com/y\nDid you mean one of:\n  - GET http://example.com/x"
    )


async def test_assert_called_with_headers_any():
    """assert_called_with() matches specific request headers and fails on wrong values."""
    url = "http://example.com/headers"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url, headers={"X-Custom": "yes"})
        m.assert_called_with(url, headers=mock.ANY, data=mock.ANY)
        m.assert_called_with(url, headers=mock.ANY, json=mock.ANY)


async def test_no_calls_message_when_nothing_recorded():
    """assert_any_call with no recorded requests reports that explicitly."""
    async with aiointercept(mock_external_urls=True) as m:
        with pytest.raises(AssertionError) as exc_info:
            m.assert_any_call("http://example.com/missing")
    assert str(exc_info.value) == ("No calls to GET http://example.com/missing\nNo requests were recorded.")


async def test_no_calls_message_suggests_recorded_calls():
    """The message suggests recorded calls even when none is a close match."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/alpha", status=200)
        await session.get("http://example.com/alpha")
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with("http://totally-different.test/zzzzzzzz")
    assert str(exc_info.value) == (
        "No calls to GET http://totally-different.test/zzzzzzzz\nDid you mean one of:\n  - GET http://example.com/alpha"
    )


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


async def test_clear_resets_state():
    """clear() empties all recorded requests and registered handlers."""
    url = "http://example.com/clear"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url)
        m.assert_called()
        m.clear()
        m.assert_not_called()
        assert len(m.handlers) == 0


async def test_clear_allows_reregistering():
    """After clear(), new handlers can be registered and matched independently."""
    url = "http://example.com/clear-reuse"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        resp = await session.get(url)
        assert resp.status == 200

        m.clear()

        m.get(url, status=201)
        resp = await session.get(url)
        assert resp.status == 201
        m.assert_called_once()


# ---------------------------------------------------------------------------
# Decorator usage
# ---------------------------------------------------------------------------


async def test_decorator_usage():
    """aiointercept can be used as a decorator; the mock is injected as the first argument."""
    url = "http://example.com/dec"
    session = ClientSession()

    @aiointercept(mock_external_urls=True)
    async def inner(m):
        m.get(url, status=200)
        resp = await session.get(url)
        assert resp.status == 200

    await inner()
    await session.close()


async def test_decorator_with_param():
    """The param= option renames the injected mock argument in the decorated function."""
    url = "http://example.com/param"
    session = ClientSession()

    @aiointercept(mock_external_urls=True, param="mock")
    async def inner(mock):
        mock.get(url, status=204)
        resp = await session.get(url)
        assert resp.status == 204

    await inner()
    await session.close()


# ---------------------------------------------------------------------------
# kwargs deprecation warning
# ---------------------------------------------------------------------------


async def test_extra_kwargs_deprecation_warning():
    """Unknown keyword arguments trigger a DeprecationWarning at construction time."""
    with pytest.warns(DeprecationWarning, match="Passing extra parameters to aiointercept"):
        m = aiointercept(mock_external_urls=True, unknown_param="foo")
    async with m:
        pass


# ---------------------------------------------------------------------------
# Multiple queued responses
# ---------------------------------------------------------------------------


async def test_multiple_queued_responses():
    """Multiple add() calls for the same URL queue responses returned in FIFO order."""
    url = "http://example.com/queue"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        m.get(url, status=201)
        m.get(url, status=202)
        statuses = [(await session.get(url)).status for _ in range(3)]
        assert statuses == [200, 201, 202]


# ---------------------------------------------------------------------------
# requests dict is populated
# ---------------------------------------------------------------------------


async def test_requests_dict_populated():
    """Every intercepted request is recorded in m.requests keyed by (method, URL)."""
    url = "http://example.com/req"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200, repeat=True)
        await session.get(url)
        await session.get(url)
        key = ("GET", URL(url))
        assert key in m.requests
        assert len(m.requests[key]) == 2


# ---------------------------------------------------------------------------
# HEAD method
# ---------------------------------------------------------------------------


async def test_head_method():
    """The head() shortcut registers a handler for HTTP HEAD requests."""
    url = "http://example.com/head"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.head(url, status=200)
        resp = await session.head(url)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Params in URL
# ---------------------------------------------------------------------------


async def test_url_with_query_params():
    """Query string parameters in the registered URL are matched correctly."""
    url = "http://queryparams.test/search?q=test&page=1"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        resp = await session.get(url)
        assert resp.status == 200
        m.assert_called_with(url)


# ---------------------------------------------------------------------------
# assert_any_call with params kwarg
# ---------------------------------------------------------------------------


async def test_assert_any_call_with_params():
    """assert_any_call() accepts a params dict and matches it against the query string."""
    url = "http://anycallparams.test/items"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url + "?key=val", status=200)
        await session.get(url, params={"key": "val"})
        m.assert_any_call(url, params={"key": "val"})


async def test_url_with_different_query_param():
    """Registering ?page=1 (repeat=True) must not serve a request to ?page=2."""
    url = "http://queryparams.test/search?q=test&page=1"
    diff_url = "http://queryparams.test/search?q=test&page=2"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200, repeat=True)
        resp = await session.get(url)
        assert resp.status == 200
        m.assert_called_with(url)
        with pytest.raises(ClientConnectionError):
            await session.get(diff_url)


# ---------------------------------------------------------------------------
# passthrough with unparseable URL → falls back to raw string as host (line 82-83)
# ---------------------------------------------------------------------------


def test_passthrough_invalid_url_fallback():
    """An unparseable passthrough entry is used as-is rather than raising."""
    m = aiointercept(mock_external_urls=True, passthrough=["not-a-valid-url:::"])
    # host is None so the raw string is used as fallback
    assert "not-a-valid-url:::" in m._passthrough_hosts


# ---------------------------------------------------------------------------
# Custom resolvers are intercepted too (resolution is funneled through
# TCPConnector._resolve_host, not the resolver classes)
# ---------------------------------------------------------------------------


async def test_custom_resolver_intercepted():
    """A user-supplied resolver must not bypass interception: for a mocked
    host, resolution short-circuits before the resolver is ever consulted."""

    class ExplodingResolver(AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            raise AssertionError("custom resolver must not be consulted for mocked hosts")

        async def close(self):
            pass

    connector = aiohttp.TCPConnector(resolver=ExplodingResolver())
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://custom-resolver.test/", status=200, body=b"ok")
        async with ClientSession(connector=connector) as session:
            resp = await session.get("http://custom-resolver.test/")
            assert resp.status == 200
            assert await resp.read() == b"ok"


# ---------------------------------------------------------------------------
# Decorator on a class method
# ---------------------------------------------------------------------------


async def test_decorator_on_class_method():
    """aiointercept decorator works correctly when applied to an instance method."""
    url = "http://classmethod.test/endpoint"
    session = ClientSession()

    class MyTest:
        @aiointercept(mock_external_urls=True)
        async def run(self, m):
            m.get(url, status=200)
            resp = await session.get(url)
            assert resp.status == 200

    await MyTest().run()
    await session.close()


# ---------------------------------------------------------------------------
# Extending an existing list of pattern handlers
# ---------------------------------------------------------------------------


async def test_pattern_handler_list_extended():
    """Adding a second pattern handler to an existing list accumulates them."""
    pattern = re.compile(r"^http://extpat\.test/p$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.add(pattern, method="GET", status=200, repeat=1)
        m.add(pattern, method="GET", status=201, repeat=1)
        r1 = await session.get("http://extpat.test/p")
        r2 = await session.get("http://extpat.test/p")
        assert r1.status == 200
        assert r2.status == 201


# ---------------------------------------------------------------------------
# Redirect following
# ---------------------------------------------------------------------------


async def test_redirect_followed():
    """A 307 response with a Location header causes aiohttp to follow the redirect."""
    base = "http://redirect.test"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(f"{base}/start", status=307, headers={"Location": f"{base}/end"})
        m.get(f"{base}/end", status=200, body=b"final")
        resp = await session.get(f"{base}/start", allow_redirects=True)
        assert resp.status == 200
        assert await resp.read() == b"final"
        assert len(resp.history) == 1


async def test_redirect_missing_mock_raises():
    """A redirect whose Location is not mocked raises ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(
            "http://redirect.test/only",
            status=307,
            headers={"Location": "http://redirect.test/missing"},
        )
        with pytest.raises(ClientConnectionError):
            await session.get("http://redirect.test/only", allow_redirects=True)


# ---------------------------------------------------------------------------
# raise_for_status variants
# ---------------------------------------------------------------------------


async def test_raise_for_status_on_response():
    """Calling raise_for_status() on a 4xx response raises ClientResponseError."""
    from aiohttp.client_exceptions import ClientResponseError

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/bad", status=400)
        resp = await session.get("http://example.com/bad")
        with pytest.raises(ClientResponseError):
            resp.raise_for_status()


async def test_raise_for_status_session_level():
    """A session with raise_for_status=True automatically raises on 4xx."""
    from aiohttp.client_exceptions import ClientResponseError

    async with ClientSession(raise_for_status=True) as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://example.com/err", status=500)
        with pytest.raises(ClientResponseError):
            await session.get("http://example.com/err")


# ---------------------------------------------------------------------------
# HTTPS scenarios
# ---------------------------------------------------------------------------


async def test_mock_https_url():
    """An https:// URL can be registered and mocked; no real TLS is used."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.patch("https://secure.test/data", status=200, body=b"secret")
        resp = await session.patch("https://secure.test/data")
        assert resp.status == 200
        assert await resp.read() == b"secret"
        m.patch("https://secure.test/data", status=200, body=b"secret")
        resp = await session.patch("https://secure.test/data")
        assert resp.status == 200
        assert await resp.read() == b"secret"


@pytest.mark.network
@network_retry
async def test_passthrough_https_explicit():
    """An https:// URL in the passthrough list reaches the real server with TLS."""
    async with (
        ClientSession() as session,
        aiointercept(mock_external_urls=True, passthrough=["https://httpbingo.org/status/201"]) as m,
    ):
        m.get("http://example.com/", status=200)
        mocked = await session.get("http://example.com/")
        assert mocked.status == 200
        real = await session.get("https://httpbingo.org/status/201")
        assert real.status == 201


@pytest.mark.network
@network_retry
async def test_passthrough_unmatched_https_no_patterns():
    """With passthrough_unmatched=True and no patterns, HTTPS goes via real DNS directly."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.get("http://example.com/mocked", status=200, body=b"mocked")
        mocked = await session.get("http://example.com/mocked")
        assert mocked.status == 200
        real = await session.get("https://httpbingo.org/status/200")
        assert real.status == 200


@pytest.mark.network
@network_retry
async def test_passthrough_unmatched_https_with_patterns():
    """With patterns active (all DNS redirected), HTTPS passthrough still works.

    _fake_ssl_context strips outer TLS and injects X-Aiointercept-Orig-Scheme so
    _dispatch can reconstruct the correct https:// URL for the bypass connector,
    which uses _BypassConnector (unpatched _get_ssl_context) for real TLS.
    """
    pattern = re.compile(r"^http://never\.matches/.*$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.add(pattern, method="GET", status=200, repeat=True)
        resp = await session.get("https://httpbingo.org/status/200")
        assert resp.status == 200


async def test_nested_mock_external_urls_instances():
    """Two nested mock_external_urls=True instances each intercept their own host,
    and the class-level patches are fully restored after both exit."""

    real_resolve_host = TCPConnector._resolve_host
    real_ssl_context = TCPConnector._get_ssl_context
    real_get = TCPConnector._get

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as outer:
        outer.get("http://outer.test/", status=200, body=b"outer", repeat=True)
        async with aiointercept(mock_external_urls=True) as inner:
            inner.get("http://inner.test/", status=200, body=b"inner")

            resp_outer = await session.get("http://outer.test/")
            assert resp_outer.status == 200
            assert await resp_outer.read() == b"outer"

            resp_inner = await session.get("http://inner.test/")
            assert resp_inner.status == 200
            assert await resp_inner.read() == b"inner"

        # After inner exits, outer still works
        resp_outer2 = await session.get("http://outer.test/")
        assert resp_outer2.status == 200

    # After both exit, class-level methods are fully restored
    assert TCPConnector._resolve_host is real_resolve_host
    assert TCPConnector._get_ssl_context is real_ssl_context
    assert TCPConnector._get is real_get


async def test_https_request_recorded_under_https_scheme():
    """When X-Aiointercept-Orig-Scheme: https is present, _dispatch must record the
    request under the https:// scheme so assert_called_with / m.requests lookups work."""
    async with aiointercept(mock_external_urls=False) as m:
        m.get("https://secure.test/data", status=200, body=b"secret")
        async with ClientSession() as session:
            # Simulate what _shared_ssl_context injects: connect directly to the
            # test server but carry the header that marks the original scheme.
            resp = await session.get(
                f"{m.server_url}/data",
                headers={"Host": "secure.test", "X-Aiointercept-Orig-Scheme": "https"},
            )
            assert resp.status == 200

        https_key = ("GET", URL("https://secure.test/data"))
        http_key = ("GET", URL("http://secure.test/data"))
        assert https_key in m.requests, "request must be recorded under https:// scheme"
        assert http_key not in m.requests, "should not appear under http:// when orig scheme is https"


async def test_duplicate_query_keys_preserved_in_callback():
    """Duplicate query params (?a=1&a=2) must both reach the callback, not be collapsed."""
    seen_query = {}

    def cb(url, **kwargs):
        seen_query.update(kwargs.get("query", {}))
        return CallbackResult(status=200)

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://qs.test/?a=1&a=2", callback=cb)
        await session.get("http://qs.test/?a=1&a=2")

    # dict(MultiDict) keeps only the last value — the bug makes this {"a": "2"}
    # The fix should preserve both values, e.g. as a list: {"a": ["1", "2"]}
    assert seen_query.get("a") == ["1", "2"], f"Expected both values for 'a', got: {seen_query.get('a')!r}"


async def test_concurrent_requests_no_race():
    """Many concurrent requests to distinct mocked URLs all resolve correctly."""

    async def random_sleep_cb(url, **kwargs):
        await asyncio.sleep(uniform(0.01, 0.1))
        return CallbackResult(body=b"ok")

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        for i in range(10):
            m.get(f"http://race.test/id-{i}", callback=random_sleep_cb)
        responses = await asyncio.gather(*[session.get(f"http://race.test/id-{i}") for i in range(10)])
        for resp in responses:
            assert resp.status == 200


# ---------------------------------------------------------------------------
# strict_headers=True in assert_called_with
# ---------------------------------------------------------------------------


async def test_assert_called_with_strict_headers_pass():
    """strict_headers=True passes when the full header map matches exactly."""
    url = "http://example.com/strict"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url, headers={"X-Token": "abc"})
        actual = dict(m.requests[("GET", URL(url))][-1].headers)
        actual.pop("x-aiointercept-orig-scheme", None)
        m.assert_called_with(url, headers=actual, strict_headers=True)


async def test_assert_called_with_strict_headers_fail():
    """strict_headers=True raises AssertionError when the header map does not match."""
    url = "http://example.com/strictfail"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        # Suppress the version-dependent auto-added headers (User-Agent,
        # Accept-Encoding, ...) so the captured header map is deterministic.
        await session.get(
            url,
            headers={"X-Token": "abc"},
            skip_auto_headers=["User-Agent", "Accept", "Accept-Encoding"],
        )
        with pytest.raises(AssertionError) as exc_info:
            m.assert_called_with(url, headers={"X-Token": "wrong"}, strict_headers=True)
    assert str(exc_info.value) == (
        "Headers mismatch: expected {'X-Token': 'wrong'}, "
        "got {'Host': 'example.com', 'X-Token': 'abc'}\n"
        "- {'X-Token': 'wrong'}\n"
        "+ {'Host': 'example.com', 'X-Token': 'abc'}"
    )


# ---------------------------------------------------------------------------
# assert_called_with extra kwargs deprecation warning
# ---------------------------------------------------------------------------


async def test_assert_called_with_extra_kwargs_deprecation():
    """Passing unknown kwargs to assert_called_with emits a DeprecationWarning."""
    url = "http://example.com/acw-kwargs"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url)
        with pytest.warns(DeprecationWarning, match="Passing extra parameters to assert_called_with"):
            m.assert_called_with(url, unknown_arg="ignored")


# ---------------------------------------------------------------------------
# data=dict with wrong Content-Type raises AssertionError
# ---------------------------------------------------------------------------


async def test_assert_called_with_data_dict_wrong_content_type():
    """assert_called_with(data=dict) raises when body was sent as JSON, not form-encoded."""
    url = "http://example.com/wrongct"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, json={"field": "value"})
        with pytest.raises(AssertionError, match="application/x-www-form-urlencoded"):
            m.assert_called_with(url, method="POST", data={"field": "value"})


# ---------------------------------------------------------------------------
# repeat < 0 raises ValueError
# ---------------------------------------------------------------------------


async def test_add_negative_repeat_raises():
    """A negative integer repeat value raises ValueError immediately."""
    async with aiointercept(mock_external_urls=True) as m:
        with pytest.raises(ValueError, match="repeat must be at least 1"):
            m.get("http://example.com/negrepeat", status=200, repeat=-1)


# ---------------------------------------------------------------------------
# content_type= explicit override
# ---------------------------------------------------------------------------


async def test_add_explicit_content_type():
    """content_type= overrides the default application/json Content-Type header."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://ct.test/", body=b"hello", content_type="text/plain")
        resp = await session.get("http://ct.test/")
        assert resp.content_type == "text/plain"


# ---------------------------------------------------------------------------
# reason= roundtrip
# ---------------------------------------------------------------------------


async def test_add_reason_phrase():
    """reason= is forwarded to the HTTP response reason phrase."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://reason.test/", status=200, reason="All Good")
        resp = await session.get("http://reason.test/")
        assert resp.reason == "All Good"


async def test_callback_result_reason():
    """CallbackResult.reason is forwarded to the HTTP response reason phrase."""

    def cb(url, **kwargs):
        return CallbackResult(status=200, reason="Callback Fine")

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://reason.test/cb", callback=cb)
        resp = await session.get("http://reason.test/cb")
        assert resp.reason == "Callback Fine"


# ---------------------------------------------------------------------------
# CallbackResult.headers non-None
# ---------------------------------------------------------------------------


async def test_callback_result_headers():
    """Non-None CallbackResult.headers are included in the response."""

    def cb(url, **kwargs):
        return CallbackResult(status=200, headers={"X-From-Callback": "yes"})

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://cbheaders.test/", callback=cb)
        resp = await session.get("http://cbheaders.test/")
        assert resp.headers.get("X-From-Callback") == "yes"


async def test_regex_callback_receives_actual_url():
    """A callback registered with a regex pattern must receive the request URL,
    not the compiled re.Pattern it was registered with."""
    seen = {}

    def callback(url, **kwargs):
        seen["url"] = url
        return CallbackResult(payload={"ok": True})

    async with aiointercept(mock_external_urls=True) as m:
        m.get(re.compile(r"http://example\.test/api/.*"), callback=callback)
        async with aiohttp.ClientSession() as session, session.get("http://example.test/api/status") as resp:
            assert await resp.json() == {"ok": True}

    assert isinstance(seen["url"], URL)
    assert str(seen["url"]) == "http://example.test/api/status"


async def test_callback_result_content_type_header():
    """A CallbackResult carrying an explicit Content-Type header must not collide
    with the default content_type and trigger an internal server error."""

    def callback(url, **kwargs):
        return CallbackResult(
            body=b"hello",
            headers={"Content-Type": "application/octet-stream"},
        )

    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://example.test/file", callback=callback)
        async with aiohttp.ClientSession() as session, session.get("http://example.test/file") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/octet-stream")
            assert await resp.read() == b"hello"


async def test_callback_result_content_type_header_case_insensitive():
    """A lower-cased content-type header must also be detected so it does not
    collide with the default content_type."""

    def callback(url, **kwargs):
        return CallbackResult(
            body=b"hello",
            headers={"content-type": "application/octet-stream"},
        )

    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://example.test/file", callback=callback)
        async with aiohttp.ClientSession() as session, session.get("http://example.test/file") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/octet-stream")
            assert await resp.read() == b"hello"


async def test_https_callback_receives_https_url():
    """Under mock_external_urls=True, a callback for an https:// URL must receive
    a URL carrying the https:// scheme — not the http:// the test server saw."""
    seen = {}

    def callback(url, **kwargs):
        seen["url"] = url
        return CallbackResult(payload={"ok": True})

    async with aiointercept(mock_external_urls=True) as m:
        m.get("https://secure.test/api/status", callback=callback)
        async with aiohttp.ClientSession() as session, session.get("https://secure.test/api/status") as resp:
            assert await resp.json() == {"ok": True}

    assert isinstance(seen["url"], URL)
    assert seen["url"].scheme == "https"
    assert str(seen["url"]) == "https://secure.test/api/status"


async def test_https_regex_callback_receives_https_url():
    """A regex callback matching an https:// URL must also receive the https:// scheme."""
    seen = {}

    def callback(url, **kwargs):
        seen["url"] = url
        return CallbackResult(payload={"ok": True})

    async with aiointercept(mock_external_urls=True) as m:
        m.get(re.compile(r"https://secure\.test/api/.*"), callback=callback)
        async with aiohttp.ClientSession() as session, session.get("https://secure.test/api/status") as resp:
            assert await resp.json() == {"ok": True}

    assert isinstance(seen["url"], URL)
    assert seen["url"].scheme == "https"
    assert str(seen["url"]) == "https://secure.test/api/status"


# ---------------------------------------------------------------------------
# exception=True (no log warning) vs exception=SomeInstance (with log warning)
# ---------------------------------------------------------------------------


async def test_exception_true_no_log_warning(caplog):
    """exception=True closes the connection without emitting a logger.warning."""
    import logging

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        with caplog.at_level(logging.WARNING, logger="aiointercept.core"):
            m.get("http://example.com/exc-true", exception=True)
        assert not caplog.records
        with pytest.raises(ClientConnectionError):
            await session.get("http://example.com/exc-true")


async def test_exception_instance_logs_warning(caplog):
    """Passing an exception instance to exception= emits a logger.warning."""
    import logging

    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        with caplog.at_level(logging.WARNING, logger="aiointercept.core"):
            m.get("http://example.com/exc-inst", exception=ValueError("boom"))
        assert caplog.records
        with pytest.raises(ClientConnectionError):
            await session.get("http://example.com/exc-inst")


# ---------------------------------------------------------------------------
# exception= registers host so DNS redirects correctly
# ---------------------------------------------------------------------------


async def test_exception_only_registration_raises_connection_error():
    """When exception= is the only registration for a host, requests raise ClientConnectionError."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.post("http://example.com/path", exception=True)
        with pytest.raises(ClientConnectionError):
            await session.post("http://example.com/path")


# ---------------------------------------------------------------------------
# clear() resets _https_hosts
# ---------------------------------------------------------------------------


async def test_clear_resets_https_hosts():
    """After clear(), a host previously seen as HTTPS is no longer treated as HTTPS."""
    async with aiointercept(mock_external_urls=False) as m:
        m.get("https://secure.test/data", status=200, body=b"secret")
        async with ClientSession() as session:
            await session.get(
                f"{m.server_url}/data",
                headers={"Host": "secure.test", "X-Aiointercept-Orig-Scheme": "https"},
            )

        assert "secure.test" in m._https_hosts

        m.clear()
        assert "secure.test" not in m._https_hosts

        m.get("http://secure.test/data", status=404, body=b"gone")
        async with ClientSession() as session:
            # Without the fix this resolves to https://secure.test/data (no handler) → 502.
            # With the fix it correctly resolves to http://secure.test/data → 404.
            resp = await session.get(
                f"{m.server_url}/data",
                headers={"Host": "secure.test"},
            )
            assert resp.status == 404


async def test_add_https_url_populates_https_hosts():
    """Registering an HTTPS URL eagerly tags the host as HTTPS, even before any
    request triggers the SSL hook. This is what lets keep-alive connections
    survive a clear() + re-add cycle."""
    async with aiointercept(mock_external_urls=True) as m:
        m.get("https://secure.test/data", status=200, body=b"ok")
        assert "secure.test" in m._https_hosts

        m.clear()
        assert "secure.test" not in m._https_hosts

        m.get("https://secure.test/data", status=200, body=b"ok")
        assert "secure.test" in m._https_hosts


async def test_clear_then_readd_https_works_over_keepalive():
    """Regression: after clear() and re-registering an HTTPS handler, a request
    that reuses the existing keep-alive connection used to fail with
    ServerDisconnectedError. aiohttp only invokes the SSL-context hook on fresh
    TCP connections, so keep-alive reuses skipped the
    X-Aiointercept-Orig-Scheme header injection that populated _https_hosts —
    and clear() had wiped the cached entry. Now add() repopulates it eagerly
    from the registered URL's scheme."""
    async with aiointercept(mock_external_urls=True) as m:
        m.post("https://secure.test/graphql", payload={"data": "first"}, repeat=True)
        async with ClientSession() as session:
            resp = await session.post("https://secure.test/graphql", json={"q": 1})
            assert await resp.json() == {"data": "first"}

            m.clear()
            m.post("https://secure.test/graphql", payload={"data": "second"})

            # Same session → reuses the keep-alive connection; the SSL hook does
            # not fire again. Without the fix this raises ServerDisconnectedError.
            resp = await session.post("https://secure.test/graphql", json={"q": 2})
            assert await resp.json() == {"data": "second"}


# ---------------------------------------------------------------------------
# passthrough_unmatched works with URL handlers (no patterns)
# ---------------------------------------------------------------------------


async def test_passthrough_unmatched_url_handler_unknown_path_proxied(real_upstream: TestServer):
    """With URL-based (non-pattern) handlers and passthrough_unmatched=True, an
    unregistered path on a registered host is proxied to the real server, not
    closed with ClientConnectionError."""
    base = upstream_base(real_upstream)
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.get(f"{base}/status/200", status=418)
        mocked = await session.get(f"{base}/status/200")
        assert mocked.status == 418
        # Same host, different path — should proxy to the real upstream, not close.
        real = await session.get(f"{base}/status/201")
        assert real.status == 201


# ---------------------------------------------------------------------------
# Caller-loop-blocked patterns (TestClient deadlock regression)
# ---------------------------------------------------------------------------


async def test_caller_loop_blocked_does_not_deadlock():
    """Regression: when the caller's event loop is blocked synchronously during
    a request — the pattern Starlette/FastAPI TestClient produces — the intercept
    server must still accept and serve the connection. The server runs on its
    own thread + loop, so this works by construction."""

    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://blocked.test/api", status=200, body=b"served")

        done = threading.Event()
        result: dict[str, object] = {}

        def _do_blocking_request() -> None:
            # A fresh event loop in a worker thread, used synchronously
            # (run_until_complete) — mirrors Starlette TestClient, which runs
            # the ASGI app in a worker thread with its own loop and blocks
            # the caller via a sync method.
            worker_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(worker_loop)

                async def _request() -> None:
                    async with ClientSession() as session:
                        resp = await session.get("http://blocked.test/api")
                        result["status"] = resp.status
                        result["body"] = await resp.read()

                worker_loop.run_until_complete(_request())
            finally:
                worker_loop.close()
                done.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_do_blocking_request)
            # Do not yield control back to the caller's loop until the worker
            # finishes — this is what TestClient effectively does by blocking
            # the caller via a sync method. If the intercept server were bound
            # to the caller's loop, this would deadlock.
            done.wait(timeout=10)
            fut.result(timeout=1)

        assert result == {"status": 200, "body": b"served"}


def test_passthrough_unmatched_without_mock_external_urls_raises():
    """passthrough_unmatched=True without mock_external_urls=True raises ValueError."""
    with pytest.raises(ValueError, match="mock_external_urls=True"):
        aiointercept(mock_external_urls=False, passthrough_unmatched=True)


async def test_assert_called_with_json_when_body_not_json():
    """assert_called_with(json=...) raises AssertionError when the actual body is not valid JSON."""
    url = "http://example.com/notjson"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, data=b"this is not json{")
        with pytest.raises(AssertionError, match="non-JSON body") as exc_info:
            m.assert_called_with(url, method="POST", json={"x": 1})
    assert str(exc_info.value) == (
        "Expected JSON body, got non-JSON body: expected {'x': 1}, got this is not json{\n"
        "- {'x': 1}\n"
        "+ this is not json{"
    )


async def test_async_callback_runs_on_server_loop_when_caller_loop_unset():
    """If _caller_loop is None, async callbacks execute directly on the server loop."""

    async def cb(url, **kwargs):
        return CallbackResult(status=205, body=b"server-loop")

    async with aiointercept(mock_external_urls=True) as m:
        m._caller_loop = None
        m.get("http://no-caller-loop.test/", callback=cb)
        async with ClientSession() as session:
            resp = await session.get("http://no-caller-loop.test/")
            assert resp.status == 205
            assert await resp.read() == b"server-loop"


async def test_shared_resolve_host_no_active_instances_uses_real_resolution():
    """With patches installed but no active instances, _shared_resolve_host
    falls through to the real implementation."""
    from aiointercept import core as ai_core

    async with aiointercept(mock_external_urls=True) as m:
        with ai_core._patch_lock:
            ai_core._active_instances.remove(m)
            ai_core._refresh_snapshot()
        connector = aiohttp.TCPConnector()
        try:
            results = await connector._resolve_host("localhost", 80)
            assert isinstance(results, list)
            assert len(results) >= 1
            assert results[0]["port"] == 80
        finally:
            await connector.close()
            with ai_core._patch_lock:
                ai_core._active_instances.append(m)
                ai_core._refresh_snapshot()


async def test_pending_tasks_cancelled_on_shutdown():
    """Tasks pending on the server loop when the context exits are cancelled and gathered."""
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://drain.test/", status=200)
        # Schedule a long-running task on the server loop that will still be pending
        # when the context manager calls loop.stop(). The shutdown finally-block must
        # cancel and gather it so the loop closes cleanly.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(60), m._server_loop)
        async with ClientSession() as session:
            resp = await session.get("http://drain.test/")
            assert resp.status == 200


async def test_server_thread_startup_failure_propagates():
    """A failure in TestServer.start_server is captured in the worker thread and re-raised."""
    from unittest.mock import patch

    from aiohttp.test_utils import TestServer

    async def boom(self, *args, **kwargs):
        raise RuntimeError("startup failed")

    with patch.object(TestServer, "start_server", boom):
        m = aiointercept(mock_external_urls=False)
        with pytest.raises(RuntimeError, match="startup failed"):
            await m.__aenter__()

    assert m._server_thread is None
    assert m._server_loop is None


async def test_aenter_failure_after_server_start_cleans_up():
    """If __aenter__ fails after the server starts, the server thread is stopped
    and the class-level patches are rolled back (no leaked refcount/state)."""
    from aiointercept import core as ai_core

    refcount_before = ai_core._patch_refcount
    resolve_host_before = TCPConnector._resolve_host

    m = aiointercept(mock_external_urls=True)

    with (
        mock.patch.object(ai_core, "_install_patches", side_effect=RuntimeError("patch install failed")),
        pytest.raises(RuntimeError, match="patch install failed"),
    ):
        await m.__aenter__()

    # start() rolls its own patch state back on failure.
    assert m._server_thread is None
    assert m._server_loop is None
    assert m not in ai_core._active_instances
    assert ai_core._patch_refcount == refcount_before
    assert TCPConnector._resolve_host is resolve_host_before


# ---------------------------------------------------------------------------
# Proxy fidelity: compressed bodies, redirects, and cookie isolation
# ---------------------------------------------------------------------------


async def test_passthrough_proxied_gzip_body_not_truncated(real_upstream: TestServer):
    """Regression: the proxy reads the upstream body decompressed, so it must
    not relay the upstream Content-Length (the *compressed* size) — aiohttp
    keeps an explicit Content-Length header as-is, truncating the response."""
    base = upstream_base(real_upstream)
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        # Claims the host so /gzip is routed to the mock server and proxied.
        m.get(f"{base}/never-called", status=200)
        resp = await session.get(f"{base}/gzip")
        assert resp.status == 200
        assert await resp.json() == GZIP_PAYLOAD


async def test_passthrough_proxied_redirect_followed_by_client(real_upstream: TestServer):
    """Regression: the proxy must relay 3xx responses untouched so the client's
    own redirect handling (allow_redirects, history) works as it would against
    the real network — instead of the proxy silently following them."""
    base = upstream_base(real_upstream)
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.get(f"{base}/never-called", status=200)
        resp = await session.get(f"{base}/redirect")
        assert resp.status == 200
        assert len(resp.history) == 1  # the client saw and followed the 302 itself


async def test_passthrough_proxied_redirect_not_followed_when_disabled(real_upstream: TestServer):
    """With allow_redirects=False on the client, a proxied 302 must reach the
    client as a 302, not as the followed-through final response."""
    base = upstream_base(real_upstream)
    async with ClientSession() as session, aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.get(f"{base}/never-called", status=200)
        resp = await session.get(f"{base}/redirect", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"].endswith("/status/200")


async def test_bypass_session_does_not_accumulate_cookies(real_upstream: TestServer):
    """The proxy must not replay cookies set by one upstream response onto
    later proxied requests — cookie state belongs to the test's own client."""
    base = upstream_base(real_upstream)
    async with aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
        m.get(f"{base}/never-called", status=200)
        # A cookie-less client: any Cookie header reaching upstream could only
        # have been added by the bypass session.
        async with ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            first = await session.get(f"{base}/cookies")
            assert (await first.json())["cookie_header"] is None
            second = await session.get(f"{base}/cookies")
            assert (await second.json())["cookie_header"] is None


# ---------------------------------------------------------------------------
# ordered_requests
# ---------------------------------------------------------------------------


async def test_ordered_requests_populated_in_arrival_order():
    """ordered_requests is a flat list of (key, AiointerceptRequest) in arrival order."""
    url_a = "http://order.test/a"
    url_b = "http://order.test/b"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url_a, status=200, repeat=True)
        m.get(url_b, status=200, repeat=True)
        await session.get(url_a)
        await session.get(url_b)
        await session.get(url_a)

    assert len(m.ordered_requests) == 3
    (k0, _), (k1, _), (k2, _) = m.ordered_requests
    assert k0 == ("GET", URL(url_a))
    assert k1 == ("GET", URL(url_b))
    assert k2 == ("GET", URL(url_a))


async def test_ordered_requests_second_element_is_request():
    """Each ordered_requests entry's second element is the AiointerceptRequest object."""
    url = "http://order.test/req"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.post(url, status=200)
        await session.post(url, json={"x": 1})

    (_, req) = m.ordered_requests[0]
    assert req.kwargs["json"] == {"x": 1}


async def test_ordered_requests_cleared_by_clear():
    """clear() empties ordered_requests."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://order.test/clear", status=200)
        await session.get("http://order.test/clear")
        assert len(m.ordered_requests) == 1
        m.clear()
        assert m.ordered_requests == []


async def test_ordered_requests_accessible_after_stop():
    """ordered_requests survives stop() so post-context assertions can read it."""
    url = "http://order.test/after-stop"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get(url, status=200)
        await session.get(url)

    assert len(m.ordered_requests) == 1
    assert m.ordered_requests[0][0] == ("GET", URL(url))


# ---------------------------------------------------------------------------
# call_count property (total)
# ---------------------------------------------------------------------------


async def test_call_count_zero_before_any_request():
    """call_count is 0 before any requests are made."""
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://cc.test/", status=200)
        assert m.call_count == 0


async def test_call_count_increments_per_request():
    """call_count increments by one for each intercepted request."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://cc.test/", status=200, repeat=True)
        for expected in range(1, 4):
            await session.get("http://cc.test/")
            assert m.call_count == expected


async def test_call_count_sums_across_urls():
    """call_count counts requests to all URLs, not just one."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://cc.test/a", status=200)
        m.post("http://cc.test/b", status=201)
        await session.get("http://cc.test/a")
        await session.post("http://cc.test/b")
        assert m.call_count == 2


# ---------------------------------------------------------------------------
# last_request property
# ---------------------------------------------------------------------------


async def test_last_request_none_before_any_call():
    """last_request is None before any requests are made."""
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://lr.test/", status=200)
        assert m.last_request is None


async def test_last_request_is_most_recent_across_urls():
    """last_request always points to the most recently intercepted request."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://lr.test/a", status=200)
        m.get("http://lr.test/b", status=200)
        await session.get("http://lr.test/a")
        after_a = m.last_request
        await session.get("http://lr.test/b")
        after_b = m.last_request

    assert after_a is not None
    assert after_b is not None
    assert after_a is not after_b
    assert after_a is m.ordered_requests[-2][1]
    assert after_b is m.ordered_requests[-1][1]


async def test_last_request_is_none_after_clear():
    """last_request returns None after clear() empties ordered_requests."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        m.get("http://lr.test/clear", status=200)
        await session.get("http://lr.test/clear")
        assert m.last_request is not None
        m.clear()
        assert m.last_request is None


# ---------------------------------------------------------------------------
# MockResponse.call_count (per-handler)
# ---------------------------------------------------------------------------


async def test_mock_response_call_count_starts_at_zero():
    """MockResponse.call_count is 0 immediately after registration."""
    async with aiointercept(mock_external_urls=True) as m:
        rsp = m.get("http://mrc.test/", status=200)
        assert isinstance(rsp, MockResponse)
        assert rsp.call_count == 0


async def test_mock_response_call_count_increments_on_hit():
    """MockResponse.call_count increments each time the mock is matched."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        rsp = m.get("http://mrc.test/", status=200, repeat=True)
        await session.get("http://mrc.test/")
        assert rsp.call_count == 1
        await session.get("http://mrc.test/")
        assert rsp.call_count == 2


async def test_mock_response_call_count_finite_repeat():
    """With repeat=N, call_count accumulates across all N slots — they share the same object."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        rsp = m.get("http://mrc.test/finite", status=200, repeat=3)
        for i in range(1, 4):
            await session.get("http://mrc.test/finite")
            assert rsp.call_count == i


async def test_mock_response_call_count_independent_per_registration():
    """Two registrations for the same URL queue independently and track their own counts."""
    url = "http://mrc.test/queue"
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        rsp1 = m.get(url, status=200)
        rsp2 = m.get(url, status=201)
        await session.get(url)
        await session.get(url)
        assert rsp1.call_count == 1
        assert rsp2.call_count == 1


async def test_mock_response_call_count_pattern_handler():
    """MockResponse.call_count works when the handler is registered with a regex pattern."""
    pattern = re.compile(r"^http://mrc\.test/items/\d+$")
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        rsp = m.add(pattern, method="GET", status=200, repeat=True)
        await session.get("http://mrc.test/items/1")
        await session.get("http://mrc.test/items/2")
        assert rsp.call_count == 2


async def test_mock_response_call_count_exception_handler():
    """MockResponse.call_count increments even when the handler closes the connection."""
    async with ClientSession() as session, aiointercept(mock_external_urls=True) as m:
        rsp = m.get("http://mrc.test/err", exception=True)
        with pytest.raises(ClientConnectionError):
            await session.get("http://mrc.test/err")
        assert rsp.call_count == 1


async def test_mock_response_returned_from_all_shortcuts():
    """Every shorthand method returns a MockResponse instance."""
    async with aiointercept(mock_external_urls=True) as m:
        for shortcut in ("get", "post", "put", "patch", "delete", "head", "options"):
            rsp = getattr(m, shortcut)("http://shortcuts.test/", status=200)
            assert isinstance(rsp, MockResponse), f"{shortcut}() must return MockResponse"


# ---------------------------------------------------------------------------
# Decorator reuse: start() clears request state between invocations
# ---------------------------------------------------------------------------


async def test_decorator_reuse_resets_request_state():
    """Each decorator invocation starts with an empty request log; counts from
    prior runs do not bleed into the next invocation."""
    url = "http://reuse.test/"
    session = ClientSession()
    counts_per_run: list[int] = []

    @aiointercept(mock_external_urls=True)
    async def run(m):
        m.get(url, status=200)
        await session.get(url)
        counts_per_run.append(m.call_count)
        m.assert_called_once()

    await run()
    await run()
    await session.close()

    assert counts_per_run == [1, 1]


async def test_start_clears_requests_on_restart():
    """Manual start()/stop() cycles reset call_count and ordered_requests."""
    m = aiointercept()
    await m.start()
    try:
        m.get(f"{m.server_url}/ping", status=200, body=b"pong")
        async with ClientSession() as session:
            await session.get(f"{m.server_url}/ping")
        assert m.call_count == 1
        assert len(m.ordered_requests) == 1
    finally:
        await m.stop()

    await m.start()
    try:
        assert m.call_count == 0
        assert len(m.ordered_requests) == 0
    finally:
        await m.stop()
