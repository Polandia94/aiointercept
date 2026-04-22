# aiointercept

A test mocking library for `aiohttp` that intercepts HTTP requests by redirecting DNS to a real local `aiohttp.web` server. Inspired by [aioresponses](https://github.com/pnuckowski/aioresponses), with an `aioresponses`-compatible API.

Unlike `aioresponses`, which patches `aiohttp` internals to short-circuit requests, `aiointercept` routes requests through a real HTTP server. This catches serialization issues, header handling, and other edge cases that pure mocking can miss.

## Installation

```bash
# pip
pip install aiointercept

# uv
uv add aiointercept

# poetry
poetry add aiointercept
```

## Requirements

- Python ≥ 3.10
- aiohttp ≥ 3.13

## Usage

### Context manager

```python
import aiohttp
from aiointercept import aiointercept

async def test_example():
    async with aiohttp.ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as m:
            m.get("http://example.com/api", payload={"hello": "world"})
            resp = await session.get("http://example.com/api")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"hello": "world"}
```

### Decorator

When used as a decorator, the `aiointercept` instance is passed as the last positional argument (or as the keyword argument named by `param`):

```python
from aiointercept import aiointercept

@aiointercept(mock_external_urls=True)
async def test_example(m):
    m.get("http://example.com/api", payload={"hello": "world"})
    async with aiohttp.ClientSession() as session:
        resp = await session.get("http://example.com/api")
        assert resp.status == 200

# Named parameter
@aiointercept(mock_external_urls=True, param="mock")
async def test_example(mock):
    mock.get("http://example.com/api", status=204)
    ...
```

### pytest fixture

```python
import pytest
import pytest_asyncio
from aiointercept import aiointercept

@pytest_asyncio.fixture
async def mock_http():
    async with aiointercept(mock_external_urls=True) as m:
        yield m

async def test_something(mock_http):
    mock_http.get("http://example.com/api", payload={"ok": True})
    async with aiohttp.ClientSession() as session:
        resp = await session.get("http://example.com/api")
        assert (await resp.json()) == {"ok": True}
```

## Constructor parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mock_external_urls` | `bool` | required | Controls how URLs are intercepted. See [Interception modes](#interception-modes) below. |
| `passthrough` | `list[str]` | `None` | List of URLs whose hosts should bypass the mock and hit the real network. Only applies when `mock_external_urls=True`. |
| `passthrough_unmatched` | `bool` | `False` | When `True`, requests with no registered handler are forwarded to the real server instead of raising a connection error. Only applies when `mock_external_urls=True`. |
| `param` | `str` | `None` | When used as a decorator, inject the mock as this keyword argument name. |

## Interception modes

`mock_external_urls` controls how `aiointercept` intercepts requests.

### `mock_external_urls=False` (recommended)

The mock server starts on `localhost`, but DNS is **not** patched. Instead, you point your application's HTTP client at the mock server directly by overriding its base URL in tests:

```python
async with aiointercept(mock_external_urls=False) as m:
    # m.server_url is the base URL of the local test server, e.g. "http://127.0.0.1:PORT"
    m.get("/api/users", payload=[{"id": 1}])

    # Pass the server URL to your app/client instead of the real base URL
    async with aiohttp.ClientSession(base_url=m.server_url) as session:
        resp = await session.get("/api/users")
        assert resp.status == 200
```

This is the preferred approach when you can configure the base URL of your HTTP client (e.g. via a fixture, environment variable, or dependency injection). It is simpler, faster, and does not touch global process state.

### `mock_external_urls=True`

Patches the DNS resolver at the process level so that every `aiohttp` request is redirected to the mock server, regardless of the hostname in the URL. Use this when you cannot easily change the base URL of the code under test — for example, when the URL is hardcoded deep inside a third-party library.

```python
async with aiointercept(mock_external_urls=True) as m:
    m.get("https://api.example.com/users", payload=[{"id": 1}])

    # No change needed in the application code — DNS is redirected globally
    async with aiohttp.ClientSession() as session:
        resp = await session.get("https://api.example.com/users")
        assert resp.status == 200
```

> **Note:** DNS patching is global for the duration of the `async with` block. Prefer `mock_external_urls=False` unless you have no other option.

## Registering mock responses

### `add(url, method, ...)`

The core method for registering a handler.

```python
m.add(
    url,                  # str, yarl.URL, or compiled re.Pattern
    method="GET",         # HTTP method (case-insensitive)
    status=200,           # response status code
    body=b"",             # raw response body (str or bytes)
    json=None,            # response body as JSON (serialized automatically)
    payload=None,         # alias for json
    headers=None,         # dict of response headers
    content_type=None,    # Content-Type header value
    repeat=False,         # True = respond indefinitely; int = respond N times
    callback=None,        # callable or coroutine receiving (url, **kwargs)
    reason=None,          # HTTP reason phrase
    exception=None,       # (deprecated) raise an exception — not fully supported
)
```

### HTTP method shortcuts

```python
m.get(url, **kwargs)
m.post(url, **kwargs)
m.put(url, **kwargs)
m.patch(url, **kwargs)
m.delete(url, **kwargs)
m.head(url, **kwargs)
m.options(url, **kwargs)
```

All shortcuts accept the same keyword arguments as `add()` (except `method`).

### Regex patterns

Use a compiled `re.Pattern` to match multiple URLs:

```python
import re
m.get(re.compile(r"^https://api\.example\.com/.*$"), payload={"ok": True})
```

### Repeat

```python
m.get(url, repeat=True)   # responds indefinitely
m.get(url, repeat=3)      # responds to the next 3 calls, then raises ClientConnectionError
```

Multiple `add()` calls for the same URL queue up responses in order:

```python
m.get(url, status=200)
m.get(url, status=201)
m.get(url, status=202)
# First call → 200, second → 201, third → 202, fourth → ClientConnectionError
```

### Callbacks

A callback receives the registered URL and the request's `headers`, `query`, and `json` as keyword arguments, and must return a `CallbackResult`:

```python
from aiointercept import CallbackResult

def my_callback(url, headers, query, json):
    return CallbackResult(status=200, payload={"echoed": json})

m.post("http://example.com/echo", callback=my_callback)

# Async callbacks are also supported:
async def async_callback(url, **kwargs):
    await asyncio.sleep(0)
    return CallbackResult(body=b"async response")

m.get("http://example.com/async", callback=async_callback)
```

`CallbackResult` fields: `status`, `body`, `payload`, `headers`, `content_type`, `reason`.

## Accessing recorded requests

All intercepted requests are stored in `m.requests`, keyed by `(METHOD, URL)`:

```python
async with aiointercept(True) as m:
    m.get("http://example.com/api")
    await session.get("http://example.com/api")

    from yarl import URL
    key = ("GET", URL("http://example.com/api"))
    request = m.requests[key][0]
    print(request.headers["User-Agent"])
    print(request.kwargs["json"])   # parsed JSON body, if any
    print(request.kwargs["query"])  # query string as dict
```

## Assertion helpers

```python
m.assert_called()                          # at least one request was made
m.assert_not_called()                      # no requests were made
m.assert_called_once()                     # exactly one request was made

m.assert_any_call(url, method="GET", params=None)
# passes if the URL was called at least once with the given method

m.assert_called_with(url, method="GET", params=None, data=None, json=None, headers=None)
# checks the first recorded call to this URL

m.assert_called_once_with(url, ...)
# equivalent to assert_called_once() + assert_called_with(...)
```

## Passthrough

### Allow specific hosts

```python
async with aiointercept(True, passthrough=["https://real-api.example.com"]) as m:
    m.get("http://mocked.example.com", payload={"mocked": True})
    # requests to real-api.example.com go through; everything else is mocked
```

### Allow all unmatched requests

```python
async with aiointercept(True, passthrough_unmatched=True) as m:
    m.get("http://mocked.example.com", payload={"mocked": True})
    # any URL without a registered handler is forwarded to the real server
```

## Known limitations

The following are known differences from `aioresponses` that may require changes when migrating:

- URL fragments (`#`) are not forwarded in requests and cannot be matched.
- Raising arbitrary exceptions via `exception=` is not supported; use `status=5xx` instead.
- The decorator must wrap an `async` function.
- DNS-based interception (`mock_external_urls=True`) does not work for requests to bare IP addresses.
- `aiohttp` may retry on connector errors, so request counts may exceed 1 in failure scenarios.
- `timeout` passthrough is not supported.

## Differences from aioresponses

| Feature | aioresponses | aiointercept |
|---|---|---|
| Context manager | sync (`with`) | async (`async with`) |
| Transport | pure mock | real aiohttp server |
| pytest fixture | sync fixture | `async` fixture (`pytest_asyncio`) |
| Exception mocking | supported | not supported |
