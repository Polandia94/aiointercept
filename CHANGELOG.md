# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `m.ordered_requests` — flat list of `(key, AiointerceptRequest)` tuples in arrival order, giving cross-URL request ordering that `m.requests` (grouped by key) does not provide.
- `m.call_count` — integer property; total requests intercepted across all URLs (`len(m.ordered_requests)`).
- `MockResponse` — object returned by `m.add()` / `m.get()` / `m.post()` etc. Its `call_count` attribute increments each time that specific registration is matched and served, enabling per-handler assertions: `rsp = m.get(url); ...; assert rsp.call_count == 2`.
- `m.last_request` — property returning the most recently intercepted `AiointerceptRequest`, or `None` if no requests have been made.

### Fixed

- `start()` now clears `m.requests` and `m.ordered_requests` at the beginning of each session. Previously, reusing the same instance across multiple invocations (decorator mode or manual `start()`/`stop()` cycles) accumulated requests from prior runs, causing `assert_called_once()` and `assert_not_called()` to report stale counts. Recorded requests remain accessible after `stop()` for post-context-manager inspection.
- `passthrough_unmatched` proxying no longer truncates compressed upstream responses: the upstream `Content-Length` (the compressed size) was relayed alongside the already-decompressed body.
- Proxied redirects are now relayed untouched (`allow_redirects=False` upstream), so the client's own redirect handling (`allow_redirects`, `response.history`) behaves as it would against the real network. Previously the proxy followed redirects itself and the client saw only the final response.
- The internal proxy session no longer accumulates cookies across proxied requests (it now uses a `DummyCookieJar`); cookie state belongs to each test's own client.
- Custom resolvers passed to `TCPConnector(resolver=...)` are now intercepted too. Resolution is funneled through a single `TCPConnector._resolve_host` patch instead of patching `ThreadedResolver.resolve` / `AsyncResolver.resolve`, which only covered those two classes.
- Calling `add()` before the server is started now raises `RuntimeError` (was `AssertionError`, which disappears under `python -O`); a URL without a host raises `ValueError`.
- `start()` now fails with a clear `RuntimeError` instead of blocking the caller's event loop forever if the server thread wedges during startup.

### Improved

- Sequential requests to a mocked host are faster: pooled keep-alive connections that already point at the mock server are reused instead of forcing a fresh TCP connection per request (connections pointing elsewhere are still discarded).
- Mock startup is faster: the passthrough proxy session is created lazily on first use instead of eagerly on every `start()`.
- `mock_external_urls=True` now installs 3 class-level patches instead of 5 (`TCPConnector._resolve_host` / `_get_ssl_context` / `_get`), and passthrough hosts keep normal DNS caching (the per-lookup cache clearing is gone — interception happens before the cache).

## [0.1.7] - 2026-06-12

### Fixed

- Allow mock.ANY on JSON, data and headers on asserts.
- Callbacks were receiving the compiled regex instead of the real URL; now every callback receives a yarl `URL`.
- Callbacks with a `Content-Type` header were raising an exception (the header collided with the default content type); now fixed.
- With `mock_external_urls=True`, a request to a mocked host is now intercepted even when the client already holds a live keep-alive connection to the real server. A class-level `TCPConnector._get` patch refuses to reuse a pooled connection for intercepted hosts, forcing a fresh (redirected) connection; passthrough hosts still reuse their connections.
- A failed `start()` (e.g. when bypass-session creation raises) now rolls back its class-level patches instead of leaking the patch refcount onto subsequent mocks.

### Improved

- DNS-cache invalidation no longer tracks `TCPConnector` instances at all. Instead of patching `TCPConnector.__init__` at import time and clearing every live connector's cache on `start()`, a class-level `_resolve_host` patch (installed only when `mock_external_urls=True`) drops the relevant cache entry per lookup. No monkey-patching happens unless `mock_external_urls=True`.
- Improved error messages

### Added

- `AiointerceptRequest.kwargs` now includes `data` for non-JSON request bodies. Thanks to [@eth2353](https://github.com/eth2353).

## [0.1.6] - 2026-06-09

### Added

- Public docs on https://aiointercept.readthedocs.io

### Improved

- The per-`start()` DNS-cache purge now tracks live `TCPConnector` instances in a `weakref.WeakSet` instead of scanning the whole heap via `gc.get_objects()`, so its cost no longer grows with the process heap size. Especially impactful for large applications and test suites. Thanks to [@agroebe](https://github.com/agroebe).

## [0.1.5] - 2026-06-02

### Added

- Public `async start()` / `async stop()` lifecycle methods. `__aenter__` / `__aexit__` now delegate to them, so the mock can be driven from setup/teardown hooks (e.g. `unittest.IsolatedAsyncioTestCase`).
- Unmatched requests now log a diagnostic `WARNING` on `aiointercept.core` explaining why nothing matched: an exhausted finite `repeat`, an `ndiff` against the closest registered `METHOD URL`, or the registered patterns.

## [0.1.4] - 2026-05-23

### Added

- Auto-discovered pytest plugin with `aiointercept_server` (session-scoped) and `aiointercept_mock` (function-scoped, calls `clear()` between tests and re-pins `_caller_loop` so async callbacks dispatch on the test's loop). Requires `pytest-asyncio`.
- `benchmarks/bench_compare.py` — standalone script comparing `aiointercept` (dns on/off) against `aioresponses` across nine scenarios. Run via `uv sync --group benchmarks && uv run python benchmarks/bench_compare.py`.

### Fixed

- `clear()` followed by re-registering an HTTPS handler no longer breaks requests that reuse an existing keep-alive connection. `add()` now records the host in `_https_hosts` eagerly when the registered URL is `https://`, so the dispatcher keeps the correct scheme even when aiohttp skips the SSL hook on reused connections.

## [0.1.3] - 2026-05-17

### Fixed

- The intercept `TestServer` no longer runs on the caller's event loop. It now starts on a dedicated daemon thread with its own loop, so callers that block their own loop between `__aenter__` and `__aexit__` (e.g. Starlette/FastAPI `TestClient`, which holds the loop during a synchronous `client.get(...)` call) can no longer deadlock the mock server. Plain mocks (`m.get(url, status=...)`) under `TestClient` now work by construction.
- During the DNS-cache purge in `_clear_existing_connector_caches`, the `isinstance(obj, aiohttp.TCPConnector)` check is now performed inside `contextlib.suppress(Exception)`, so a stray object whose `__class__` lookup raises during `gc.get_objects()` iteration no longer aborts the purge for the remaining connectors.

### Changed

- Async user callbacks registered via `callback=` are now executed on the caller's event loop (the loop active when `__aenter__` was called), even though the server runs on its own loop. This preserves prior semantics: `asyncio.Event`, `asyncio.Queue`, `asyncio.Lock`, and other loop-bound primitives shared between the test and the callback continue to work. Sync callbacks are unaffected and continue to run on the server loop.
- Package metadata modernized: `license` is now an SPDX expression (`"MIT"`) and the deprecated `License :: OSI Approved :: MIT License` trove classifier was removed.

### Internal

- Expanded the `ruff` lint rule set (`UP`, `B`, `C4`, `SIM`, `N`, `RUF`, `PT`, `TCH`, `ASYNC`, `PERF`, `RET`, `ARG`) with `line-length = 120` and `target-version = "py310"`, and reformatted `aiointercept/core.py` accordingly: PEP 585 `type[X]` over `typing.Type[X]`, string-form `cast("X", ...)` annotations, `contextlib.suppress` instead of `try/except/pass`, and consolidated imports. No behaviour change.
- Bumped the `mypy` dev dependency to `>=2.0.0,<2.1.0`.

### Known limitations

- The narrow combination of an **async callback** *and* a caller loop that is fully blocked (e.g. an async `callback=` inside a Starlette `TestClient` request) still deadlocks: the callback is scheduled onto the caller's loop, which cannot run it while it is blocked. Plain mocks under `TestClient` are unaffected.

## [0.1.2] - 2026-05-12

### Added

- `AiointerceptRequest` is now exported from the top-level `aiointercept` package so users can type-annotate recorded requests without reaching into `aiointercept.core`.

### Fixed

- `clear()` now also resets the internal `_https_hosts` set, so a host previously seen with HTTPS traffic is no longer incorrectly treated as HTTPS after `clear()` is called.
- `exception=` (any truthy value) now correctly registers the target host in `_host_list` before returning, ensuring DNS is redirected to the mock server by design rather than by the fallback path.
- `passthrough_unmatched=True` now proxies unmatched paths for URL-registered hosts (not just pattern-registered ones). Previously, a registered host with an unknown path would close the connection even when `passthrough_unmatched=True`.
- `_host_list` is now a `set` instead of a `list`, preventing duplicate host entries when the same URL is registered multiple times.
- Passing `passthrough_unmatched=True` without `mock_external_urls=True` now raises `ValueError` at construction time instead of being silently ignored.

### Changed

- `mock_external_urls` now defaults to `False`, making it an optional parameter. Callers that omit it get the recommended no-DNS-patching mode.
- Renamed `AiointerceptRequest._captured_body` → `AiointerceptRequest.captured_body` (now public). 

### Internal

- Renamed `AiointercepRequest` → `AiointerceptRequest` (added missing `t`).
- Renamed `AiointerceptRequstKwargs` → `AiointerceptRequestKwargs` (fixed `Requst` → `Request`).
- Replaced the `Exception`-class-as-sentinel pattern with a named `_CloseConnection` sentinel for the "close transport" handler marker.
- Added comments on the 502 fallback responses in `_dispatch` clarifying they only surface if `transport.close()` does not take effect.

## [0.1.1] - 2026-05-04

Initial public release.

### Added

- Real `aiohttp.web` test server — requests travel through an actual HTTP stack instead of being short-circuited in memory.
- Two interception modes controlled by the `mock_external_urls` constructor argument:
  - `False` — server starts on localhost; point your client at `m.server_url` directly. No global state patched.
  - `True` — patches `ThreadedResolver`/`AsyncResolver` at the class level so any `aiohttp` request is redirected, regardless of hostname.
- HTTPS interception (`mock_external_urls=True`): patches `TCPConnector._get_ssl_context` to strip TLS for intercepted hosts and reconstructs the original `https://` URL server-side via an injected `X-Aiointercept-Orig-Scheme` header.
- `aioresponses`-compatible registration API: `m.get/post/put/patch/delete/head/options`, `m.add`, `CallbackResult`.
- Regex pattern matching via compiled `re.Pattern` URLs.
- Sync and async callback support; callbacks receive `url`, `headers`, `query`, and `json`.
- `repeat=True` (unlimited) and `repeat=N` (finite) response queuing; multiple `add()` calls for the same URL queue responses in order.
- `passthrough` — list of hosts to bypass the mock and hit the real network.
- `passthrough_unmatched=True` — forward unregistered requests to the real server instead of raising `ClientConnectionError`.
- `m.requests` — dict keyed by `(METHOD, yarl.URL)` recording every intercepted request, with parsed `headers`, `query`, and `json` in `request.kwargs`.
- `m.clear()` — reset registered handlers and recorded requests without tearing down the server.
- `m.server_url` — base URL of the local test server (available inside the `async with` block).
- Assertion helpers: `assert_called`, `assert_not_called`, `assert_called_once`, `assert_any_call`, `assert_called_with`, `assert_called_once_with`.
- SSL context caching to avoid redundant per-host lookups.
- Decorator usage with optional `param=` to name the injected mock argument.

### Known limitations

- **Bare IP addresses** (`http://1.2.3.4/path`) are not intercepted when `mock_external_urls=True` because DNS patching has no effect on numeric addresses.
- **`exception=`** only closes the connection, surfacing a `ClientConnectionError` on the client. Raising arbitrary exception types is not supported.
- **`timeout=` passthrough** is not supported.
- **`CallbackResult(response_class=)`** is silently ignored.
- request `**kwargs` contains only `headers`, `query`, and `json` — not the full `aiohttp` request kwargs set.
