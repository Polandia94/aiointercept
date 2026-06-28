# aiointercept

[![PyPI](https://img.shields.io/pypi/v/aiointercept.svg)](https://pypi.org/project/aiointercept/)
[![Python](https://img.shields.io/pypi/pyversions/aiointercept.svg)](https://pypi.org/project/aiointercept/)
[![Docs](https://readthedocs.org/projects/aiointercept/badge/?version=latest)](https://aiointercept.readthedocs.io/)
[![CI](https://github.com/Polandia94/aiointercept/actions/workflows/ci.yml/badge.svg)](https://github.com/Polandia94/aiointercept/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Mock `aiohttp` HTTP requests by routing them through a real `aiohttp.web` test server. Inspired by `aioresponses`, with a largely compatible API.

```python
import aiohttp
from aiointercept import aiointercept

async with aiointercept() as m:
    m.get(f"{m.server_url}/users", payload=[{"id": 1}])

    async with aiohttp.ClientSession() as session:
        resp = await session.get(f"{m.server_url}/users")
        assert await resp.json() == [{"id": 1}]
```

## Why aiointercept?

Testing code that makes HTTP requests usually means either hitting a real server (slow, fragile, requires network) or replacing the HTTP layer with fake objects (fast, but disconnected from reality).

`aiointercept` takes a third path: it starts a real `aiohttp.web` server on localhost and redirects your client's requests to it — either by pointing the client at `m.server_url` directly, or by patching the DNS resolver so existing URLs are transparently intercepted. Your code runs its full HTTP stack; only the remote endpoint is replaced.

- **Real serialization.** Headers, body encoding, and content-type negotiation all go through the actual aiohttp stack.
- **Inspectable requests.** Callbacks receive a real `aiohttp.web.Request` — read the body, headers, and query params the server saw.
- **Minimal patching.** The default mode touches nothing globally. When you need to intercept hardcoded URLs, only the DNS resolver is patched, so redirects and connection pooling still behave as in production.

## Installation

```bash
pip install aiointercept
```

**Requirements:** Python ≥ 3.10, aiohttp ≥ 3.13.

## Documentation

Full documentation is at **[aiointercept.readthedocs.io](https://aiointercept.readthedocs.io/)**:

- [Quickstart](https://aiointercept.readthedocs.io/en/latest/quickstart.html) — context manager, decorator, pytest fixture, and `start()`/`stop()` usage.
- [Usage guide](https://aiointercept.readthedocs.io/en/latest/usage.html) — interception modes, registering responses, regex patterns, callbacks, and passthrough.
- [Assertions](https://aiointercept.readthedocs.io/en/latest/assertions.html) — `assert_called_with` and friends.
- [API reference](https://aiointercept.readthedocs.io/en/latest/api.html) — every public class and method.

## Sharing the server across tests

Starting and stopping a server for every test adds up. `aiointercept` ships an
auto-discovered pytest plugin (requires `pytest-asyncio`) that starts the server
**once per session** and hands each test a cleared mock via the
`aiointercept_mock` fixture:

```python
async def test_users(aiointercept_mock):
    m = aiointercept_mock
    m.get(f"{m.server_url}/users", payload=[{"id": 1}])

    async with aiohttp.ClientSession() as session:
        resp = await session.get(f"{m.server_url}/users")
        assert await resp.json() == [{"id": 1}]
```

The fixture calls `m.clear()` between tests, so registered handlers and recorded
requests never leak from one test to the next. The session-scoped server itself
is exposed as `aiointercept_server` if you need it directly.

## Coming from aioresponses?

`aiointercept` aims to be a near drop-in replacement. The [migration guide](https://aiointercept.readthedocs.io/en/latest/migrating.html) ([MIGRATING.md](MIGRATING.md)) covers every breaking change. If you hit an incompatibility it doesn't cover, please [open an issue](https://github.com/Polandia94/aiointercept/issues).

## Contributing

```bash
uv sync --group dev --group tests   # install everything
uv run pytest tests/                 # run the suite
uv run ruff check .                  # lint
uv run mypy aiointercept             # type check
```

Pre-commit hooks run `ruff` and `mypy` on every commit; do not bypass them with `--no-verify`.

## License

`aiointercept` is released under the [MIT License](LICENSE).

## Attribution

Built on ideas and API conventions from [aioresponses](https://github.com/pnuckowski/aioresponses) by Pawel Nuckowski (MIT License). [tests/test_aioresponse.py](tests/test_aioresponse.py) is a lightly adapted port of the original test suite, used to verify compatibility.
