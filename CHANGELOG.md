# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
