# Benchmark results

Snapshot from [`bench_compare.py`](bench_compare.py). Per-op times, median of 3 runs. Ratios vs `aioresponses`.

Environment: Python 3.14.3, aiohttp 3.13.5, Windows 11, Intel i7. Re-run with `uv sync --group benchmarks && uv run python benchmarks/bench_compare.py`.

| Scenario                              |    N | aioresponses | aii(dns=F) | aii(dns=T) | F/aio | T/aio |
|---------------------------------------|-----:|-------------:|-----------:|-----------:|------:|------:|
| lifecycle (CM + GET + assert)         |   20 |      4.01 ms |    8.95 ms |   71.16 ms |  2.2x | 17.7x |
| fixture (1 CM, N x GET+assert+clear)  |   50 |      1.45 ms |    3.81 ms |    4.98 ms |  2.6x |  3.4x |
| sequential GET                        |  500 |     841.6 us |    1.82 ms |    1.68 ms |  2.2x |  2.0x |
| concurrent GET (batch=50)             |  500 |     969.2 us |    1.15 ms |    1.56 ms |  1.2x |  1.6x |
| POST + assert_called_with             |  200 |     847.3 us |    1.89 ms |    2.43 ms |  2.2x |  2.9x |
| regex pattern URL                     |  200 |     877.9 us |    1.45 ms |    2.12 ms |  1.7x |  2.4x |
| large payload (1 MB)                  |   20 |      1.01 ms |    5.51 ms |   20.44 ms |  5.4x | 20.2x |
| HTTPS                                 |   20 |      1.36 ms |        N/A |   12.87 ms |    -- |  9.4x |
| many distinct mocked URLs             |  500 |      1.14 ms |    1.94 ms |    1.95 ms |  1.7x |  1.7x |

## Reading the columns

- **aioresponses** — transport-level patch, request never leaves the process.
- **aii(dns=F)** — `aiointercept()` with client pointed at the real localhost server.
- **aii(dns=T)** — `aiointercept(mock_external_urls=True)` — same server plus `ThreadedResolver` / `AsyncResolver` / `_get_ssl_context` patches installed on `__aenter__`, removed on `__aexit__`.

The slowdown is the price of real serialization and a real `aiohttp.web.Request` in callbacks.

## What dominates each scenario

- **lifecycle** — server startup + DNS-patch install/uninstall per iteration. Worst case for `aii`. Use the [`aiointercept_mock` fixture](../README.md#sharing-the-server-across-tests) to amortize.
- **fixture** — what the pytest plugin enables. Gap drops from ~17x to ~3x once startup is paid once.
- **concurrent GET** — most competitive (1.2x): localhost round-trip is dominated by event-loop scheduling.
- **large payload** — real bytes on a real socket; 5.4x is the wire-transfer cost.
- **HTTPS** — `dns=F` is N/A (HTTP-only server, no CA pinned); `dns=T` pays ~9x for SSL-context faking.
- **many URLs** — both `aii` modes scale identically with handler count.