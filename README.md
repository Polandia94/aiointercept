# aiointercept

A test mocking library for `aiohttp` that intercepts HTTP requests by redirecting DNS to a real local `aiohttp.web` server. Inspired by [aioresponses](https://github.com/pnuckowski/aioresponses), with a compatible API.

Unlike `aioresponses`, which patches `aiohttp` internals to short-circuit requests, `aiointercept` routes requests through a real HTTP server — catching serialization issues, header handling, and other edge cases that pure mocking can miss.

## Installation

```bash
pip install aiointercept
# or: uv add aiointercept / poetry add aiointercept
```

**Requirements:** Python ≥ 3.10, aiohttp ≥ 3.13

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
            assert await resp.json() == {"hello": "world"}
```

### Decorator

The `aiointercept` instance is passed as the last positional argument (or the kwarg named by `param`):

```python
from aiointercept import aiointercept

@aiointercept(mock_external_urls=True)
async def test_example(m):
    m.get("http://example.com/api", payload={"hello": "world"})
    ...

@aiointercept(mock_external_urls=True, param="mock")
async def test_named(mock):
    mock.get("http://example.com/api", status=204)
    ...
```

### pytest fixture

Add `asyncio_mode = "auto"` to your `pyproject.toml` and use an async fixture:

```python
import pytest_asyncio
from aiointercept import aiointercept

@pytest_asyncio.fixture
async def mock_http():
    async with aiointercept(mock_external_urls=True) as m:
        yield m

async def test_something(mock_http):
    mock_http.get("http://example.com/api", payload={"ok": True})
    ...
```

## Constructor parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mock_external_urls` | `bool` | required | See [Interception modes](#interception-modes). |
| `passthrough` | `list[str]` | `None` | Hosts that bypass the mock and hit the real network. Only with `mock_external_urls=True`. |
| `passthrough_unmatched` | `bool` | `False` | Forward unmatched requests to the real server instead of raising. Only with `mock_external_urls=True`. |
| `param` | `str` | `None` | Inject the mock under this kwarg name when used as a decorator. |

## Interception modes

### `mock_external_urls=False` (recommended)

The server starts on `localhost` but DNS is not patched. Point your client at `m.server_url` directly:

```python
async with aiointercept(mock_external_urls=False) as m:
    m.get("/api/users", payload=[{"id": 1}])
    async with aiohttp.ClientSession(base_url=m.server_url) as session:
        resp = await session.get("/api/users")
```

Preferred when you can configure the client's base URL — simpler, faster, no global state changes.

### `mock_external_urls=True`

Patches the DNS resolver at the process level so every `aiohttp` request is redirected to the mock server. Use this when you cannot change the URL of the code under test (e.g. a hardcoded URL inside a third-party library).

```python
async with aiointercept(mock_external_urls=True) as m:
    m.get("https://api.example.com/users", payload=[{"id": 1}])
    async with aiohttp.ClientSession() as session:
        resp = await session.get("https://api.example.com/users")
```

> DNS patching is global for the duration of the block and does **not** work for bare IP addresses.

## Registering mock responses

### `add(url, method, ...)`

```python
m.add(
    url,                  # str, yarl.URL, or compiled re.Pattern
    method="GET",
    status=200,
    body=b"",             # raw response body
    json=None,            # response body as JSON (serialized automatically)
    payload=None,         # alias for json
    headers=None,
    content_type=None,
    repeat=False,         # True = indefinitely; int = N times
    callback=None,        # callable or coroutine: (url, **kwargs) → CallbackResult
    reason=None,
    exception=None,       # truthy → close connection (ClientConnectionError)
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

### Regex patterns

```python
import re
m.get(re.compile(r"^https://api\.example\.com/.*$"), payload={"ok": True})
```

### Repeat and queuing

```python
m.get(url, repeat=True)   # responds indefinitely
m.get(url, repeat=3)      # responds 3 times, then raises ClientConnectionError

# Multiple add() calls queue responses in order:
m.get(url, status=200)
m.get(url, status=201)
# First call → 200, second → 201, third → ClientConnectionError
```

### Callbacks

```python
from aiointercept import CallbackResult

def my_callback(url, headers, query, json):
    return CallbackResult(status=200, payload={"echoed": json})

m.post("http://example.com/echo", callback=my_callback)

async def async_callback(url, **kwargs):
    return CallbackResult(body=b"async response")

m.get("http://example.com/async", callback=async_callback)
```

### `CallbackResult` fields

| Field | Type | Default | Description |
|---|---|---|---|
| `status` | `int` | `200` | HTTP response status code |
| `body` | `str \| bytes` | `""` | Raw response body |
| `payload` | `Any` | `None` | Response body as JSON |
| `headers` | `dict[str, str] \| None` | `None` | Extra response headers |
| `content_type` | `str` | `"application/json"` | Content-Type header |
| `reason` | `str \| None` | `None` | HTTP reason phrase |

## Instance attributes

### `m.server_url`

Base URL of the local test server, e.g. `"http://127.0.0.1:54321"`. Use with `mock_external_urls=False`.

### `m.requests`

Dict mapping `(METHOD: str, URL: yarl.URL)` to a list of intercepted `AiointercepRequest` that inherits from `aiohttp.web.Request` objects:

```python
from yarl import URL

key = ("GET", URL("http://example.com/api"))
req = m.requests[key][0]
req.headers["User-Agent"]
req.kwargs["json"]    # parsed JSON body
req.kwargs["query"]   # query string as dict[str, list[str]]
req.kwargs["headers"] # raw request headers
```

URLs are normalized (fragment stripped, query params sorted).

### `m.clear()`

Resets all registered handlers and recorded requests.

## Assertion helpers

```python
m.assert_called()
m.assert_not_called()
m.assert_called_once()

m.assert_any_call(url, method="GET", params=None)
m.assert_called_with(url, method="GET", params=None, data=None, json=None, headers=None, strict_headers=False)
m.assert_called_once_with(url, ...)
```

`assert_called_with` checks the most recent call to the URL. Pass `strict_headers=True` to compare the full header map instead of just the keys you provide.

## Passthrough

```python
# Specific hosts bypass the mock:
async with aiointercept(True, passthrough=["https://real-api.example.com"]) as m:
    ...

# All unmatched requests go to the real server:
async with aiointercept(True, passthrough_unmatched=True) as m:
    ...
```

## Migrating from aioresponses

`aiointercept` is a near drop-in replacement. Key differences:

| Feature | aioresponses | aiointercept |
|---|---|---|
| Context manager | sync (`with`) | async (`async with`) |
| Transport | pure mock | real `aiohttp.web` server |
| pytest fixture | sync | `async` (`pytest_asyncio`) |
| `mock_external_urls` | always mock | **required** constructor arg |
| `exception=` | raises given exception | `ClientConnectionError` only |
| `CallbackResult(response_class=)` | used | silently ignored, not needed |
| request `**kwargs` keys | full request kwargs | `headers`, `query`, `json` only |
| `call_count` / `call_args_list` | available | not implemented |
| Bare-IP DNS interception | works | not supported |
| `timeout=` passthrough | supported | not supported |

`assert_called_with` / `assert_called_once_with` silently ignore client-only kwargs like `ssl=` and `timeout=` (they are not observable on the wire) and emit a `DeprecationWarning`. Remove those arguments when migrating.

### Compatibility policy

The goal is to keep `aiointercept` as a near drop-in replacement for `aioresponses`. If you find an incompatibility not listed in the table above, please open an issue — it will be documented, and if there is a reasonable way to resolve it, it will be attempted.

### Roadmap

- **More assertion helpers** — `call_count`, `call_args_list`, and compare with only some attributes.
- **Richer `mock_external_urls=False` mode** — additional convenience and introspection for tests that point the client directly at `m.server_url`, without any DNS patching.

## Attribution

Built on ideas and API conventions from [aioresponses](https://github.com/pnuckowski/aioresponses) by Pawel Nuckowski (MIT License). [tests/test_aioresponse.py](tests/test_aioresponse.py) is a lightly adapted port of the original test suite used to verify compatibility.
