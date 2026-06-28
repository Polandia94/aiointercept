# Benchmark results

Snapshot from [`bench_compare.py`](bench_compare.py). Per-op times, median of 3 runs. Ratios vs `aioresponses`.

Environment: Python 3.14.3, aiohttp 3.14.1, Windows 11, Intel i7. Re-run with `uv sync --group benchmarks && uv run python benchmarks/bench_compare.py`.
Aioresponses is not compatible with aiohttp 3.14.1, so I used a local copy with the fix merged.

| Scenario                              |    N | aioresponses | aii(dns=F) | aii(dns=T) | F/aio | T/aio |
|---------------------------------------|-----:|-------------:|-----------:|-----------:|------:|------:|
| lifecycle (CM + GET + assert)         |   20 |      7.93 ms |   10.06 ms |   15.91 ms |  1.3x |  2.0x |
| fixture (1 CM, N x GET+assert+clear)  |   50 |      2.46 ms |    4.84 ms |    8.80 ms |  2.0x |  3.6x |
| sequential GET                        |  500 |      1.21 ms |    1.32 ms |    2.81 ms |  1.1x |  2.3x |
| concurrent GET (batch=50)             |  500 |      1.44 ms |   998.8 us |    1.36 ms |  0.7x |  0.9x |
| POST + assert_called_with             |  200 |      1.51 ms |    2.60 ms |    2.42 ms |  1.7x |  1.6x |
| regex pattern URL                     |  200 |      1.57 ms |    1.56 ms |    1.53 ms |  1.0x |  1.0x |
| large payload (1 MB)                  |   20 |      1.52 ms |    7.82 ms |   10.74 ms |  5.1x |  7.1x |
| HTTPS                                 |   20 |      1.43 ms |        N/A |    1.64 ms |    -- |  1.1x |
| many distinct mocked URLs             |  500 |      1.55 ms |    1.54 ms |    2.24 ms |  1.0x |  1.4x |

## Reading the columns

- **aioresponses** — transport-level patch, request never leaves the process.
- **aii(dns=F)** — `aiointercept()` with client pointed at the real localhost server.
- **aii(dns=T)** — `aiointercept(mock_external_urls=True)` — same server plus `ThreadedResolver` / `AsyncResolver` / `_get_ssl_context` patches installed on `__aenter__`, removed on `__aexit__`.

The slowdown is the price of real serialization and a real `aiohttp.web.Request` in callbacks.

## What dominates each scenario

- **lifecycle** — server startup + DNS-patch install/uninstall per iteration. Highest absolute cost, but per-iteration startup dominates both backends so the ratio stays modest (2.0x). Use the [`aiointercept_mock` fixture](../README.md#sharing-the-server-across-tests) to amortize.
- **fixture** — what the pytest plugin enables: startup paid once, then steady-state GET+assert+clear. The 3.6x here is the real per-request overhead once per-test startup is out of the picture.
- **concurrent GET** — most competitive: `aii` actually edges out `aioresponses` (0.7x / 0.9x) because the localhost round-trip is dominated by event-loop scheduling both backends pay.
- **large payload** — real bytes on a real socket; 5.1x is the wire-transfer cost.
- **HTTPS** — `dns=F` is N/A (HTTP-only server, no CA pinned); `dns=T` adds only ~1.1x for SSL-context faking.
- **many URLs** — `dns=F` scales identically to `aioresponses` (1.0x); `dns=T` adds ~1.4x for the resolver/SSL patches.