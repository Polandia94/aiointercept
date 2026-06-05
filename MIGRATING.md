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
def test_something(m):
    ...

# After — must be async
@aiointercept(mock_external_urls=True)
async def test_something(m):
    ...
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
- `assert_called_with(url, ssl=False)` → drop client-only kwargs like `ssl=`, `timeout=`; they are silently ignored but a `DeprecationWarning` is emitted listing the dropped keys
- Callbacks only receive `headers`, `query`, and `json` (no client-side kwargs)
- Bare IP addresses are not intercepted
- `call_count` / `call_args_list` are not implemented
- `timeout=` passthrough is not supported

Once your tests are passing, consider migrating to `mock_external_urls=False` (the default) — no DNS patching, cleaner isolation.
