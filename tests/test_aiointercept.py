"""Tests for aiointercept.core targeting ~100% coverage."""

import re
import pytest
import aiohttp
from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientConnectionError
from yarl import URL
from aiohttp.resolver import ThreadedResolver, AsyncResolver
import asyncio
from random import uniform

from aiointercept import aiointercept, CallbackResult

# ---------------------------------------------------------------------------
# Basic mock_external_urls=True (DNS patched) vs False (direct to server)
# ---------------------------------------------------------------------------


async def test_mock_external_urls_true_basic():
    """DNS is patched so requests to example.com are intercepted and served by the mock."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/hello", status=200, body=b"hi")
            resp = await session.get("http://example.com/hello")
            assert resp.status == 200
            assert await resp.read() == b"hi"


async def test_mock_external_urls_false_uses_server_host_port():
    """Without DNS patching we connect directly to server_host:server_port."""
    async with aiointercept(mock_external_urls=False) as m:
        m.add(f"{m.server_url}/api", method="GET", status=201, body=b"direct")
        url = f"http://{m.server_host}:{m.server_port}/api"
        async with ClientSession() as session:
            resp = await session.get(url)
        assert resp.status == 201
        assert await resp.read() == b"direct"


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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.add(url_input, method="GET", status=200, body=b"ok")
            resp = await session.get("http://api.test/items")
            assert resp.status == 200


async def test_add_pattern():
    """A compiled regex pattern matches any URL that satisfies the expression."""
    pattern = re.compile(r"^http://api\.test/items/\d+$")
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.add(pattern, method="GET", status=200, body=b"item")
            resp = await session.get("http://api.test/items/42")
            assert resp.status == 200
            assert await resp.read() == b"item"


async def test_pattern_no_match_raises():
    """A URL that does not satisfy the registered pattern raises ClientConnectionError."""
    pattern = re.compile(r"^http://api\.test/items/\d+$")
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.add(pattern, method="GET", status=200)
            with pytest.raises(ClientConnectionError):
                await session.get("http://api.test/items/abc")


# ---------------------------------------------------------------------------
# HTTP method shortcuts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,shortcut",
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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            getattr(m, shortcut)(url, status=200)
            resp = await session.request(method, url)
            assert resp.status == 200


# ---------------------------------------------------------------------------
# Response body variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected_body",
    [
        ({"body": b"bytes"}, b"bytes"),
        ({"body": "string"}, b"string"),
        ({"json": {"a": 1}}, b'{"a": 1}'),
        ({"payload": {"x": 2}}, b'{"x": 2}'),
    ],
)
async def test_response_body_variants(kwargs, expected_body):
    """body (bytes or str), json, and payload are all serialised to the expected bytes."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://body.test/", **kwargs)
            resp = await session.get("http://body.test/")
            assert await resp.read() == expected_body


# ---------------------------------------------------------------------------
# repeat parameter
# ---------------------------------------------------------------------------


async def test_repeat_true_infinite():
    """repeat=True keeps the handler registered indefinitely for any number of calls."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://repeat.test/", status=200, repeat=True)
            for _ in range(5):
                resp = await session.get("http://repeat.test/")
                assert resp.status == 200


async def test_repeat_false_once():
    """repeat=False (default) consumes the handler after a single call; subsequent calls raise."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://repeat.test/once", status=200, repeat=False)
            resp = await session.get("http://repeat.test/once")
            assert resp.status == 200
            with pytest.raises(ClientConnectionError):
                await session.get("http://repeat.test/once")


@pytest.mark.parametrize("n", [2, 3])
async def test_repeat_integer(n):
    """repeat=N allows exactly N calls before the handler is exhausted."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://repeat.test/n", status=200, repeat=n)
            for _ in range(n):
                resp = await session.get("http://repeat.test/n")
                assert resp.status == 200
            with pytest.raises(ClientConnectionError):
                await session.get("http://repeat.test/n")


async def test_repeat_zero():
    """repeat=0 is treated identically to repeat=False (respond exactly once)."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://repeat.test/once", status=200, repeat=0)
            resp = await session.get("http://repeat.test/once")
            assert resp.status == 200
            with pytest.raises(ClientConnectionError):
                await session.get("http://repeat.test/once")


# ---------------------------------------------------------------------------
# Pattern repeat
# ---------------------------------------------------------------------------


async def test_pattern_repeat_true():
    """A pattern handler with repeat=True serves every matching URL without expiring."""
    pattern = re.compile(r"^http://pat\.test/.*$")
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.add(pattern, method="GET", status=200, repeat=True)
            for i in range(3):
                resp = await session.get(f"http://pat.test/path{i}")
                assert resp.status == 200


async def test_pattern_repeat_integer():
    """A pattern handler with repeat=2 is consumed after exactly two matching requests."""
    pattern = re.compile(r"^http://pat\.test/path$")
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
        with pytest.raises(ValueError):
            m.add(pattern, method="GET", status=201, repeat=1)


async def test_url_repeat_error_mixing_repeat_true_and_list():
    """Adding a finite repeat after an infinite repeat=True on the same URL raises ValueError."""
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://mix.test/path", status=200, repeat=True)
        with pytest.raises(ValueError):
            m.get("http://mix.test/path", status=201, repeat=1)


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


async def test_sync_callback():
    """A synchronous callback function is invoked and its CallbackResult is returned."""

    def cb(url, **kwargs):
        return CallbackResult(status=202, body=b"cb-sync")

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://cb.test/", callback=cb)
            resp = await session.get("http://cb.test/")
            assert resp.status == 202
            assert await resp.read() == b"cb-sync"


async def test_async_callback():
    """An async callback coroutine is awaited and its CallbackResult is returned."""

    async def cb(url, **kwargs):
        return CallbackResult(status=203, body=b"cb-async")

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://cb.test/async", callback=cb)
            resp = await session.get("http://cb.test/async")
            assert resp.status == 203


async def test_callback_with_payload():
    """A callback that returns a payload dict is JSON-serialised in the response body."""

    def cb(url, **kwargs):
        return CallbackResult(status=200, payload={"key": "val"})

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://cb.test/payload", callback=cb)
            resp = await session.get("http://cb.test/payload")
            data = await resp.json()
            assert data == {"key": "val"}


# ---------------------------------------------------------------------------
# Passthrough (mock_external_urls=True with specific hosts allowed through)
# ---------------------------------------------------------------------------


async def test_passthrough_host_is_allowed():
    """Passthrough host resolves normally (hits real network)."""
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough=["http://httpbin.org/status/200"]
        ) as m:
            m.get("http://example.com/", status=200)
            mocked = await session.get("http://example.com/")
            assert mocked.status == 200
            real = await session.get("http://httpbin.org/status/200")
            assert real.status == 200


async def test_registered_host_missing_path_raises():
    """Registered host with an unregistered path raises ClientConnectionError."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/registered", status=200)
            with pytest.raises(ClientConnectionError):
                await session.get("http://example.com/not-registered")


# ---------------------------------------------------------------------------
# passthrough_unmatched
# ---------------------------------------------------------------------------


async def test_passthrough_unmatched_allows_real_requests():
    """Requests without a registered handler pass through to the network."""
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough_unmatched=True
        ) as m:
            m.get("http://example.com/mocked", status=200, body=b"mocked")
            mocked = await session.get("http://example.com/mocked")
            assert mocked.status == 200
            real = await session.get("http://httpbin.org/status/201")
            assert real.status == 201


async def test_passthrough_unmatched_false_raises_for_unknown():
    """With passthrough_unmatched=False, any unregistered host raises ClientConnectionError."""
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough_unmatched=False
        ) as m:
            m.get("http://example.com/", status=200)
            with pytest.raises(ClientConnectionError):
                await session.get("http://unregistered.test/foo")


async def test_passthrough_unmatched_with_pattern_proxies_unmatched():
    """
    With a pattern registered, DNS redirects ALL hosts to the test server.
    Unmatched requests fall into _dispatch's passthrough branch, which now
    uses a _BypassResolver so the inner connector calls real DNS instead of
    looping back to the test server.
    """
    pattern = re.compile(r"^http://pat\.test/specific$")
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough_unmatched=True
        ) as m:
            m.add(pattern, method="GET", status=200, repeat=True)
            # Matched: mock response
            resp_mocked = await session.get("http://pat.test/specific")
            assert resp_mocked.status == 200
            # Unmatched: proxied via real DNS to the actual server
            resp_real = await session.get("http://httpbin.org/status/201")
            assert resp_real.status == 201


# ---------------------------------------------------------------------------
# Failure scenarios
# ---------------------------------------------------------------------------


async def test_no_handler_returns_connection_error():
    """A request to a path with no registered handler raises ClientConnectionError."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as _m:
            _m.get("http://example.com/", status=200)
            with pytest.raises(ClientConnectionError):
                await session.get("http://example.com/missing")


async def test_exception_parameter_causes_connection_error():
    """exception= registers a handler that closes the connection, raising ClientConnectionError."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/err", exception=Exception("boom"), status=200)
            with pytest.raises(ClientConnectionError):
                await session.get("http://example.com/err")


async def test_method_mismatch_raises():
    """A POST to a GET-only handler raises ClientConnectionError."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/meth", status=200)
            with pytest.raises(ClientConnectionError):
                await session.post("http://example.com/meth")


async def test_add_without_server_raises():
    """Calling add() before entering the context manager (no server) raises AssertionError."""
    m = aiointercept(mock_external_urls=True)
    with pytest.raises(AssertionError):
        m.add("http://example.com/", method="GET", status=200)


# ---------------------------------------------------------------------------
# Assert helpers
# ---------------------------------------------------------------------------


async def test_assert_called_and_not_called():
    """assert_called() and assert_not_called() reflect whether any request was made."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/assert", status=200)
            m.assert_not_called()
            await session.get("http://example.com/assert")
            m.assert_called()
            with pytest.raises(AssertionError):
                m.assert_not_called()


async def test_assert_called_once():
    """assert_called_once() passes after exactly one request and fails after a second."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/any", status=200)
            await session.get("http://example.com/any")
            m.assert_any_call("http://example.com/any")
            with pytest.raises(AssertionError):
                m.assert_any_call("http://example.com/other")


async def test_assert_called_with_json():
    """assert_called_with() matches the JSON body sent in the request."""
    url = "http://example.com/json"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.post(url, status=200)
            await session.post(url, json={"x": 1})
            m.assert_called_with(url, method="POST", json={"x": 1})
            with pytest.raises(AssertionError):
                m.assert_called_with(url, method="POST", json={"x": 2})


async def test_assert_called_with_data_bytes():
    """assert_called_with() matches raw bytes sent as the request body."""
    url = "http://example.com/data"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.post(url, status=200)
            await session.post(url, data=b"rawbytes")
            m.assert_called_with(url, method="POST", data=b"rawbytes")


async def test_assert_called_with_data_string():
    """assert_called_with() matches a plain string sent as the request body."""
    url = "http://example.com/strdata"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.post(url, status=200)
            await session.post(url, data="hello")
            m.assert_called_with(url, method="POST", data="hello")


async def test_assert_called_with_data_dict():
    """assert_called_with() matches a form-encoded dict sent as the request body."""
    url = "http://example.com/formdata"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.post(url, status=200)
            await session.post(url, data={"field": "value"})
            m.assert_called_with(url, method="POST", data={"field": "value"})


async def test_assert_called_with_headers():
    """assert_called_with() matches specific request headers and fails on wrong values."""
    url = "http://example.com/headers"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get(url, status=200)
            await session.get(url, headers={"X-Custom": "yes"})
            m.assert_called_with(url, headers={"X-Custom": "yes"})
            with pytest.raises(AssertionError):
                m.assert_called_with(url, headers={"X-Custom": "no"})


async def test_assert_called_once_with():
    """assert_called_once_with() passes when the URL was called exactly once."""
    url = "http://example.com/oncewith"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get(url, status=200)
            await session.get(url)
            m.assert_called_once_with(url)


async def test_assert_called_with_wrong_url():
    """assert_called_with() raises AssertionError when the URL does not match what was called."""
    url = "http://example.com/x"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get(url, status=200)
            await session.get(url)
            with pytest.raises(AssertionError):
                m.assert_called_with("http://example.com/y")


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


async def test_clear_resets_state():
    """clear() empties all recorded requests and registered handlers."""
    url = "http://example.com/clear"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get(url, status=200)
            await session.get(url)
            m.assert_called()
            m.clear()
            m.assert_not_called()
            assert len(m.handlers) == 0


async def test_clear_allows_reregistering():
    """After clear(), new handlers can be registered and matched independently."""
    url = "http://example.com/clear-reuse"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
    with pytest.warns(DeprecationWarning):
        m = aiointercept(mock_external_urls=True, unknown_param="foo")
    async with m:
        pass


# ---------------------------------------------------------------------------
# Multiple queued responses
# ---------------------------------------------------------------------------


async def test_multiple_queued_responses():
    """Multiple add() calls for the same URL queue responses returned in FIFO order."""
    url = "http://example.com/queue"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.head(url, status=200)
            resp = await session.head(url)
            assert resp.status == 200


# ---------------------------------------------------------------------------
# Params in URL
# ---------------------------------------------------------------------------


async def test_url_with_query_params():
    """Query string parameters in the registered URL are matched correctly."""
    url = "http://queryparams.test/search?q=test&page=1"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get(url + "?key=val", status=200)
            await session.get(url, params={"key": "val"})
            m.assert_any_call(url, params={"key": "val"})


async def test_url_with_different_query_param():
    """Registering ?page=1 (repeat=True) must not serve a request to ?page=2."""
    url = "http://queryparams.test/search?q=test&page=1"
    diff_url = "http://queryparams.test/search?q=test&page=2"
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
# _clear_all_connector_caches swallows exceptions (lines 262-263)
# ---------------------------------------------------------------------------


async def test_clear_connector_cache_exception_swallowed():
    """If clear_dns_cache() raises, it should be swallowed silently."""
    connector = aiohttp.TCPConnector()

    def raising_clear():
        raise RuntimeError("simulated dns cache error")

    connector.clear_dns_cache = raising_clear  # type: ignore[method-assign]
    session = ClientSession(connector=connector)
    # Should not raise even though clear_dns_cache raises
    async with aiointercept(mock_external_urls=True) as m:
        m.get("http://clearcache.test/", status=200)
        resp = await session.get("http://clearcache.test/")
        assert resp.status == 200
    await session.close()


# ---------------------------------------------------------------------------
# Decorator on a class method (line 187 branch)
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
# Extending an existing list of pattern handlers (lines 444-447)
# ---------------------------------------------------------------------------


async def test_pattern_handler_list_extended():
    """Adding a second pattern handler to an existing list accumulates them."""
    pattern = re.compile(r"^http://extpat\.test/p$")
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get(f"{base}/start", status=307, headers={"Location": f"{base}/end"})
            m.get(f"{base}/end", status=200, body=b"final")
            resp = await session.get(f"{base}/start", allow_redirects=True)
            assert resp.status == 200
            assert await resp.read() == b"final"
            assert len(resp.history) == 1


async def test_redirect_missing_mock_raises():
    """A redirect whose Location is not mocked raises ClientConnectionError."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
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

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/bad", status=400)
            resp = await session.get("http://example.com/bad")
            with pytest.raises(ClientResponseError):
                resp.raise_for_status()


async def test_raise_for_status_session_level():
    """A session with raise_for_status=True automatically raises on 4xx."""
    from aiohttp.client_exceptions import ClientResponseError

    async with ClientSession(raise_for_status=True) as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/err", status=500)
            with pytest.raises(ClientResponseError):
                await session.get("http://example.com/err")


# ---------------------------------------------------------------------------
# HTTPS scenarios
# ---------------------------------------------------------------------------


async def test_mock_https_url():
    """An https:// URL can be registered and mocked; no real TLS is used."""
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("https://secure.test/data", status=200, body=b"secret")
            resp = await session.get("https://secure.test/data")
            assert resp.status == 200
            assert await resp.read() == b"secret"


async def test_passthrough_https_explicit():
    """An https:// URL in the passthrough list reaches the real server with TLS."""
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough=["https://httpbin.org/status/201"]
        ) as m:
            m.get("http://example.com/", status=200)
            mocked = await session.get("http://example.com/")
            assert mocked.status == 200
            real = await session.get("https://httpbin.org/status/201")
            assert real.status == 201


async def test_passthrough_unmatched_https_no_patterns():
    """With passthrough_unmatched=True and no patterns, HTTPS goes via real DNS directly."""
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough_unmatched=True
        ) as m:
            m.get("http://example.com/mocked", status=200, body=b"mocked")
            mocked = await session.get("http://example.com/mocked")
            assert mocked.status == 200
            real = await session.get("https://httpbin.org/status/200")
            assert real.status == 200


async def test_passthrough_unmatched_https_with_patterns():
    """With patterns active (all DNS redirected), HTTPS passthrough still works.

    _fake_ssl_context strips outer TLS and injects X-Aiointercept-Orig-Scheme so
    _dispatch can reconstruct the correct https:// URL for the bypass connector,
    which uses _BypassConnector (unpatched _get_ssl_context) for real TLS.
    """
    pattern = re.compile(r"^http://never\.matches/.*$")
    async with ClientSession() as session:
        async with aiointercept(
            mock_external_urls=True, passthrough_unmatched=True
        ) as m:
            m.add(pattern, method="GET", status=200, repeat=True)
            resp = await session.get("https://httpbin.org/status/200")
            assert resp.status == 200


async def test_nested_mock_external_urls_instances():
    """Two nested mock_external_urls=True instances each intercept their own host,
    and the class-level resolver is fully restored after both exit."""

    real_threaded = ThreadedResolver.resolve
    real_async = AsyncResolver.resolve

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as outer:
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
    assert ThreadedResolver.resolve is real_threaded
    assert AsyncResolver.resolve is real_async


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
        assert http_key not in m.requests, (
            "should not appear under http:// when orig scheme is https"
        )


async def test_duplicate_query_keys_preserved_in_callback():
    """Duplicate query params (?a=1&a=2) must both reach the callback, not be collapsed."""
    seen_query = {}

    def cb(url, **kwargs):
        seen_query.update(kwargs.get("query", {}))
        return CallbackResult(status=200)

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://qs.test/?a=1&a=2", callback=cb)
            await session.get("http://qs.test/?a=1&a=2")

    # dict(MultiDict) keeps only the last value — the bug makes this {"a": "2"}
    # The fix should preserve both values, e.g. as a list: {"a": ["1", "2"]}
    assert seen_query.get("a") == ["1", "2"], (
        f"Expected both values for 'a', got: {seen_query.get('a')!r}"
    )


async def test_concurrent_requests_no_race():
    """Many concurrent requests to distinct mocked URLs all resolve correctly."""

    async def random_sleep_cb(url, **kwargs):
        await asyncio.sleep(uniform(0.01, 0.1))
        return CallbackResult(body=b"ok")

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            for i in range(10):
                m.get(f"http://race.test/id-{i}", callback=random_sleep_cb)
            responses = await asyncio.gather(
                *[session.get(f"http://race.test/id-{i}") for i in range(10)]
            )
            for resp in responses:
                assert resp.status == 200
