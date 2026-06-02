# aiointercept

[![PyPI](https://img.shields.io/pypi/v/aiointercept.svg)](https://pypi.org/project/aiointercept/)
[![Python](https://img.shields.io/pypi/pyversions/aiointercept.svg)](https://pypi.org/project/aiointercept/)
[![CI](https://github.com/Polandia94/aiointercept/actions/workflows/ci.yml/badge.svg)](https://github.com/Polandia94/aiointercept/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Mock `aiohttp` HTTP requests by routing them through a real `aiohttp.web` test server. Inspired by `aioresponses`, with a largely compatible API.

```python
async with aiointercept() as m:
    m.get(f"{m.server_url}/users", payload=[{"id": 1}])

    async with aiohttp.ClientSession() as session:
        resp = await session.get(f"{m.server_url}/users")
        assert await resp.json() == [{"id": 1}]
```

---

## Why aiointercept?

Testing code that makes HTTP requests usually means either hitting a real server (slow, fragile, requires network) or replacing the HTTP layer with fake objects (fast, but disconnected from reality).

`aiointercept` takes a third path: it starts a real `aiohttp.web` server on localhost and redirects your client's requests to it — either by pointing the client at `m.server_url` directly, or by patching the DNS resolver so existing URLs are transparently intercepted. Your code runs its full HTTP stack; only the remote endpoint is replaced.

This gives you:

- **Real serialization.** Headers, body encoding, and content-type negotiation all go through the actual aiohttp stack, so bugs that only appear during serialization are caught.
- **Inspectable requests.** Callbacks receive a real `aiohttp.web.Request` — you can read the body, headers, query params, and anything else the server would see.
- **Minimal patching.** The default mode touches nothing globally — your client just talks to a local server. When you need to intercept hardcoded URLs, only the DNS resolver is patched (not aiohttp's request pipeline), so concurrent requests, redirects, and connection pooling still behave as in production.

---

## Installation

```bash
pip install aiointercept
```


**Requirements:** Python ≥ 3.10, aiohttp ≥ 3.13

> **Coming from aioresponses?**
[MIGRATING.md](https://github.com/Polandia94/aiointercept/blob/main/MIGRATING.md) covers every breaking change: context manager usage, fixture patterns, URL registration, `exception=`, callbacks, and assertion helpers.

The goal is a near drop-in replacement. If you find an incompatibility not covered by the migration guide, please [open an issue](https://github.com/Polandia94/aiointercept/issues).

---

## Getting Started

### Context manager

```python
import aiohttp
from aiointercept import aiointercept

async def test_get_user():
    async with aiointercept() as m:
        m.get(f"{m.server_url}/user/1", payload={"id": 1, "name": "Alice"})

        async with aiohttp.ClientSession() as session:
            resp = await session.get(f"{m.server_url}/user/1")
            assert resp.status == 200
            assert await resp.json() == {"id": 1, "name": "Alice"}
```

### Decorator

The `aiointercept` instance is injected as the last positional argument, or under the name given by `param`:

```python
from aiointercept import aiointercept

@aiointercept()
async def test_create_post(m):
    m.post(f"{m.server_url}/posts", status=201, payload={"id": 42})
    ...

@aiointercept(param="mock")
async def test_named(mock):
    mock.get(f"{mock.server_url}/feed", payload=[])
    ...
```

### pytest fixture

```python
import pytest_asyncio
from aiointercept import aiointercept

@pytest_asyncio.fixture
async def mock_http():
    async with aiointercept() as m:
        yield m

async def test_something(mock_http):
    mock_http.get(f"{mock_http.server_url}/items", payload=[{"id": 1}])
    ...
```

> `@pytest_asyncio.fixture` works in all pytest-asyncio modes. If you use `asyncio_mode = "auto"` in `pyproject.toml`, plain `@pytest.fixture` works too.

### Manual `start()` / `stop()`

For frameworks with their own setup/teardown hooks (e.g. `unittest.IsolatedAsyncioTestCase`) you can drive the lifecycle explicitly.
```python
import unittest
from aiointercept import aiointercept

class TestApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = aiointercept()
        await self.helper.start()

    async def asyncTearDown(self):
        await self.helper.stop()

    async def test_user(self):
        self.helper.get(f"{self.helper.server_url}/user/1", payload={"id": 1})
        ...
```

---

## Interception Modes

### `mock_external_urls=False` (default)

The server starts on localhost but DNS is **not** patched. Point your client directly at `m.server_url`. This is the safest mode — no global state is modified.

```python
async with aiointercept() as m:
    m.get(f"{m.server_url}/api/users", payload=[{"id": 1}])

    async with aiohttp.ClientSession() as session:
        resp = await session.get(f"{m.server_url}/api/users")
        assert resp.status == 200
```

Use `m.server_url` as a `base_url` to keep your code clean:

```python
async with aiointercept() as m:
    m.get(f"{m.server_url}/api/users", payload=[{"id": 1}])

    async with aiohttp.ClientSession(base_url=m.server_url) as session:
        resp = await session.get("/api/users")
```

### `mock_external_urls=True`

Patches the DNS resolver at the **process level** so every `aiohttp` request is redirected to the mock server — even those made by third-party libraries you cannot modify.

```python
async with aiointercept(mock_external_urls=True) as m:
    m.get("https://api.stripe.com/v1/charges", payload={"data": []})

    # Code under test calls the real Stripe URL internally
    result = await billing_service.list_charges()
    assert result == []
```

> DNS patching affects the whole process for the duration of the block. It does not intercept requests to bare IP addresses.

---

## Registering Mock Responses

### `add(url, method, ...)`

```python
m.add(
    url,                    # str | yarl.URL | re.Pattern
    method="GET",           # HTTP method (case-insensitive)
    status=200,
    body=b"",               # raw response body (str is UTF-8 encoded)
    json=None,              # serialized to JSON, overrides body
    payload=None,           # alias for json
    headers=None,           # extra response headers
    content_type=None,      # overrides Content-Type
    repeat=False,           # True = infinite; int N = exactly N times
    callback=None,          # callable or coroutine → CallbackResult
    reason=None,            # HTTP reason phrase
    exception=None,         # truthy → close connection (ClientConnectionError)
)
```

### HTTP Method Shortcuts

```python
m.get(url, **kwargs)
m.post(url, **kwargs)
m.put(url, **kwargs)
m.patch(url, **kwargs)
m.delete(url, **kwargs)
m.head(url, **kwargs)
m.options(url, **kwargs)
```

All shortcuts forward their keyword arguments to `add()`.

### Regex Patterns

Pass a compiled `re.Pattern` to match a family of URLs:

```python
import re

pattern = re.compile(r"^https://api\.example\.com/users/\d+$")
m.get(pattern, payload={"id": 1, "name": "Alice"})

# Matches https://api.example.com/users/1, /users/42, etc.
```

### Repeat and Response Queuing

```python
# Respond to every request (indefinite):
m.get(url, repeat=True, payload={"ok": True})

# Respond exactly 3 times, then raise ClientConnectionError:
m.get(url, repeat=3, status=200)

# Queue different responses by calling add() multiple times:
m.post(url, status=201, payload={"created": True})
m.post(url, status=409, payload={"error": "conflict"})
# First POST → 201, second POST → 409, third POST → ClientConnectionError
```

### Callbacks

Use a callback when the response depends on the request:

```python
from aiointercept import aiointercept, CallbackResult

def echo_callback(url, *, headers, query, json, **kwargs):
    return CallbackResult(status=200, payload={"echo": json})

async def test_echo():
    async with aiointercept() as m:
        m.post(f"{m.server_url}/echo", callback=echo_callback)
        ...
```

Async callbacks are also supported:

```python
async def async_callback(url, **kwargs):
    await asyncio.sleep(10)
    return CallbackResult(body=b"async response")

async def test_slow():
    async with aiointercept() as m:
        m.get(f"{m.server_url}/slow", callback=async_callback)
        ...
```

#### `CallbackResult`

| Field | Type | Default | Description |
|---|---|---|---|
| `status` | `int` | `200` | Response status code |
| `body` | `str \| bytes` | `""` | Raw response body |
| `payload` | `Any` | `None` | Response body serialized to JSON (overrides `body`) |
| `headers` | `dict[str, str] \| None` | `None` | Extra response headers |
| `content_type` | `str` | `"application/json"` | `Content-Type` header |
| `reason` | `str \| None` | `None` | HTTP reason phrase |

---

## Passthrough

Let specific hosts or all unmatched requests reach the real network. Only available with `mock_external_urls=True`.

```python
# Specific hosts bypass the mock:
async with aiointercept(mock_external_urls=True, passthrough=["https://real-api.example.com"]) as m:
    m.get("https://mocked.example.com/data", payload={"mocked": True})
    # Requests to real-api.example.com go to the real server.

# All unmatched requests go to the real server:
async with aiointercept(mock_external_urls=True, passthrough_unmatched=True) as m:
    m.get("https://mocked.example.com/data", payload={"mocked": True})
    # Any other URL is proxied to the real network.
```

---

## Assertion Helpers

`aiointercept` records every intercepted request in `m.requests` and provides a set of assertion helpers inspired by `unittest.mock`.

```python
m.assert_called()              # at least one request was made
m.assert_not_called()          # no requests were made
m.assert_called_once()         # exactly one request across all URLs

m.assert_any_call(url, method="GET", params=None)
m.assert_called_with(url, method="GET", params=None, data=None, json=None, headers=None, strict_headers=False)
m.assert_called_once_with(url, ...)
```

`assert_called_with` inspects the **most recent** call to the given URL.

```python
# Check the JSON body of the last POST to /orders:
m.assert_called_with(
    "https://api.example.com/orders",
    method="POST",
    json={"item": "book", "qty": 2},
)

# Check specific headers (subset by default):
m.assert_called_with(url, headers={"Authorization": "Bearer token123"})

# Require the full header map to match exactly:
m.assert_called_with(url, headers={"X-Custom": "value"}, strict_headers=True)

# Check form-encoded body:
m.assert_called_with(url, method="POST", data={"username": "alice", "password": "s3cr3t"})
```

---

## Inspecting Requests

All intercepted requests are stored in `m.requests`, keyed by `(METHOD, normalized_url)`:

```python
from yarl import URL

key = ("POST", URL("https://api.example.com/orders"))
req = m.requests[key][-1]   # most recent request

req.captured_body            # raw bytes body
req.kwargs["json"]           # parsed JSON body (or None)
req.kwargs["query"]          # dict[str, list[str]] — preserves duplicate keys
req.kwargs["headers"]        # raw request headers (multidict)
```

URLs are normalized: fragments are stripped and query parameters are sorted.

---

## Sharing the server across tests

Each `async with aiointercept()` block starts a fresh `aiohttp.web` test server. The bundled pytest plugin (auto-discovered, requires `pytest-asyncio`) amortizes that with a session-scoped server and a function-scoped wrapper that calls `clear()` between tests.

```python
async def test_get_user(aiointercept_mock):
    m = aiointercept_mock
    m.get(f"{m.server_url}/user/1", payload={"id": 1, "name": "Alice"})

    async with aiohttp.ClientSession() as session:
        resp = await session.get(f"{m.server_url}/user/1")
        assert resp.status == 200
    m.assert_called_with(f"{m.server_url}/user/1", method="GET")
```

Fixtures: `aiointercept_server` (session) and `aiointercept_mock` (function — use this in tests). Override `aiointercept_server` in your `conftest.py` to customize.

Caveats:

- Defaults to `mock_external_urls=False`. To intercept URLs for the whole session, override `aiointercept_server` and pass `mock_external_urls=True` — note that the DNS/SSL patches are installed at the class level and stay live for every aiohttp call in the test process (this blocks real network calls).

---

## Constructor Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mock_external_urls` | `bool` | `False` | When `True`, patches the DNS resolver so external URLs are intercepted. When `False`, only requests to `m.server_url` are intercepted. |
| `passthrough` | `list[str] \| None` | `None` | Hosts whose requests bypass the mock and reach the real network. Requires `mock_external_urls=True`. |
| `passthrough_unmatched` | `bool` | `False` | Proxy all unmatched requests to the real network. Requires `mock_external_urls=True`. |
| `param` | `str \| None` | `None` | Kwarg name under which the mock is injected when used as a decorator. |

---

## Instance Attributes

### `m.server_url`

Base URL of the local test server, e.g. `"http://127.0.0.1:54321"`. Available after entering the context manager. Useful with `mock_external_urls=False`.

### `m.requests`

```python
m.requests: dict[tuple[str, URL], list[AiointerceptRequest]]
```

Maps `(METHOD, URL)` to a list of all intercepted requests in the order they were received.

### `m.clear()`

Resets all registered handlers and the recorded request log. Useful between test cases when reusing a fixture.

---

## Contributing

```bash
# Install all dependencies
uv sync --group dev --group tests

# Run the test suite
uv run pytest tests/

# Lint and format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy aiointercept
```

Pre-commit hooks run `ruff` and `mypy` on every commit. Do not bypass them with `--no-verify`.

---

## License

`aiointercept` is released under the [MIT License](LICENSE).

---

## Attribution

Built on ideas and API conventions from [aioresponses](https://github.com/pnuckowski/aioresponses) by Pawel Nuckowski (MIT License). [tests/test_aioresponse.py](tests/test_aioresponse.py) is a lightly adapted port of the original test suite, used to verify compatibility.
