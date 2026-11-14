# Migrating from aioresponses to aiointercept

Replace `aioresponses` with `aiointercept(mock_external_urls=True)` — URLs and response registration stay the same.

## Steps

**1. Install the package**

```bash
pip install aiointercept
```

**2. Update imports**

```python
from aiointercept import aiointercept, CallbackResult
```

**3. Switch context manager to `async with` and add `mock_external_urls=True`**

If you were using the context manager form, switch to async context manager and add `mock_external_urls=True`:

```python
# Before
with aioresponses() as m:
    m.get("https://api.example.com/data", payload={"ok": True})

# After
async with aiointercept(mock_external_urls=True) as m:
    m.get("https://api.example.com/data", payload={"ok": True})
```

**4. Switch decorator adding `mock_external_urls=True`**

If you were using the decorator form, add `mock_external_urls=True` — everything else stays the same:

```python
# Before
@aioresponses()
async def test_something(m):
    m.get("https://api.example.com/data", payload={"ok": True})


# After
@aiointercept(mock_external_urls=True)
async def test_something(m):
    m.get("https://api.example.com/data", payload={"ok": True})
```

If your decorated function was **not** async, make it async:

```python
# Before — aioresponses ran the event loop for you
@aioresponses()
def test_something(m): ...


# After — must be async
@aiointercept(mock_external_urls=True)
async def test_something(m): ...
```

**5. Make pytest fixtures async**

If you were using aioresponses on a sync fixture, make it async. Use `pytest_asyncio.fixture` for that:

```python
# Before
@pytest.fixture
def mock_http():
    with aioresponses() as m:
        yield m


# After
@pytest_asyncio.fixture
async def mock_http():
    async with aiointercept(mock_external_urls=True) as m:
        yield m
```

## Differences to be aware of

- `exception=SomeError(...)` → use `exception=True` (always raises `ClientConnectionError`)
- `add(response_class=X)` → drop `response_class=`, it is ignored
- `assert_called_with(url, ssl=False)` → drop client-only kwargs like `ssl=`, `timeout=`; they are silently ignored but a `DeprecationWarning` is emitted
- Callbacks only receive `headers`, `query`, `data`, and `json` (no client-side kwargs)
- Exceptions raised inside a callback do not reach the code that made the request: the client gets an HTTP 500 response and the traceback is logged on the `aiohttp.server` logger (aioresponses propagates the exception). To make the client see a connection error, register `exception=True` instead
- `passthrough` accepts hosts without ports, not URLs, and matches by host rather than URL prefix: without exact-URL registrations on that host, `passthrough=["api.example.com"]` lets every path and port on `api.example.com` through (aioresponses matches URL prefixes). An exact-URL registration takes precedence and causes the whole host to be intercepted; unmatched URLs then fail unless `passthrough_unmatched=True`
- `request.kwargs` is a deprecated compatibility shim — read captured requests natively instead (see below)
- Bare IP addresses are not intercepted
- `call_args_list` is not implemented; use `m.requests` (grouped by `(METHOD, URL)`) or `m.ordered_requests` (arrival order). `m.call_count` (all requests) and each registration's `call_count` are available
- `timeout=` passthrough is not supported
- Idempotent requests are retried by aiohttp after a disconnect (see below)

## Automatic retries of idempotent requests

aiointercept answers over a real connection. When it wants a request to fail (an
`exception=True` registration, or no matching handler), it closes the connection, and
aiohttp's client retries the request once when the server disconnects before responding.
aiohttp only does this for idempotent methods: GET, HEAD, OPTIONS, TRACE, PUT and DELETE.
POST and PATCH are not retried.

This has two consequences for idempotent methods:

- One unmatched GET reaches the mock twice. It appears twice in `m.requests`, `m.call_count`
  is 2 and the "no handler" warning is logged twice. `m.assert_called_once()` fails after a
  single unmatched GET. An unmatched POST reaches the mock once.
- With a failure queued before a success, the first call does not raise. The retry consumes
  the success registration and the caller gets 200:

  ```python
  m.get(url, exception=True)
  m.get(url, payload={"ok": True})

  await session.get(url)  # 200, not ClientConnectionError
  ```

  With aioresponses the first call raises, because aioresponses never opens a connection.

Workarounds:

- Register the failure twice, so it also absorbs the retry:

  ```python
  boom = m.get(url, exception=True, repeat=2)  # first attempt + aiohttp's automatic retry
  m.get(url, payload={"ok": True})

  with pytest.raises(aiohttp.ClientConnectionError):
      await session.get(url)  # raises
  resp = await session.get(url)  # 200
  ```

  After this, `boom.call_count == 2` and `m.call_count == 3`.
- Use a non-idempotent method (POST or PATCH) in the test, where the code under test allows it.
  Those requests are never retried.

An `exception=True` registration on its own already raises for every method, because the
retry finds no handler and is closed too. Only the counts and the "fail once, then succeed"
sequence are affected.

## Reading captured requests

`request.kwargs` is a compatibility shim slated for deprecation. Captured requests are real
`aiohttp.web.Request` objects, so read them natively instead:

| `kwargs` key | native accessor |
| --- | --- |
| `kwargs["headers"]` | `request.headers` |
| `kwargs["query"]` | `request.query` (scalar per key; `request.query.getall(k)` for repeated keys) |
| `kwargs["data"]` | `request.captured_body` |
| `kwargs["json"]` | `await request.json()` |

For example, a query-param assertion becomes:

```diff
- assert request.kwargs["query"]["foo"] == ["bar"]
+ assert request.query["foo"] == "bar"
```

Once your tests are passing, consider migrating to `mock_external_urls=False` (the default) — no DNS patching, cleaner isolation.
