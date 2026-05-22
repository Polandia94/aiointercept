"""Benchmark aiointercept (DNS-patch off/on) against aioresponses.

Standalone script — not collected by pytest. Run with:

    uv sync --group benchmarks
    uv run python benchmarks/bench_compare.py [--quick] [--reps N] [--scenario name]

Times reported are per-op (total / N); ratios are vs aioresponses.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import statistics
import time
import warnings
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from aiointercept import aiointercept

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logging.basicConfig(level=logging.ERROR)
warnings.filterwarnings("ignore", category=ResourceWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="aioresponses.*")

try:
    from aioresponses import aioresponses
except ImportError as exc:
    raise SystemExit("aioresponses is not installed. Run: uv sync --group benchmarks") from exc

HOST = "http://example.com"
LARGE_BODY = b"x" * (1024 * 1024)
PATTERN_HOST_RE = re.compile(r"^http://example\.com/items/\d+$")


@asynccontextmanager
async def _backend(kind: str) -> AsyncIterator[tuple[Any, str]]:
    """Enter the requested mock backend; yield ``(mock, base_url)``."""
    if kind == "aio":
        with aioresponses() as m:
            yield m, HOST
    elif kind == "aii_off":
        async with aiointercept() as m:
            yield m, m.server_url
    elif kind == "aii_on":
        async with aiointercept(mock_external_urls=True) as m:
            yield m, HOST
    else:  # pragma: no cover
        raise ValueError(kind)


async def s_lifecycle(kind: str, n: int) -> None:
    for _ in range(n):
        async with _backend(kind) as (m, base):
            url = f"{base}/api"
            m.get(url, status=200, payload={"ok": True})
            async with aiohttp.ClientSession() as session:
                resp = await session.get(url)
                assert resp.status == 200
            m.assert_called_with(url, method="GET")


async def s_fixture(kind: str, n: int) -> None:
    async with _backend(kind) as (m, base):
        url = f"{base}/api"
        for _ in range(n):
            m.get(url, status=200, payload={"ok": True})
            async with aiohttp.ClientSession() as session:
                resp = await session.get(url)
                assert resp.status == 200
            m.assert_called_with(url, method="GET")
            m.clear()


async def s_seq(kind: str, n: int) -> None:
    async with _backend(kind) as (m, base):
        url = f"{base}/api"
        m.get(url, status=200, payload={"ok": True}, repeat=True)
        async with aiohttp.ClientSession() as session:
            for _ in range(n):
                resp = await session.get(url)
                assert resp.status == 200


async def s_conc(kind: str, n: int, *, batch: int) -> None:
    async with _backend(kind) as (m, base):
        url = f"{base}/api"
        m.get(url, status=200, payload={"ok": True}, repeat=True)
        async with aiohttp.ClientSession() as session:
            for _ in range(n // batch):
                await asyncio.gather(*[session.get(url) for _ in range(batch)])


async def s_post(kind: str, n: int) -> None:
    async with _backend(kind) as (m, base):
        url = f"{base}/items"
        m.post(url, status=201, payload={"ok": True}, repeat=True)
        body = {"name": "widget", "qty": 3}
        async with aiohttp.ClientSession() as session:
            for _ in range(n):
                resp = await session.post(url, json=body)
                assert resp.status == 201
        m.assert_called_with(url, method="POST", json=body)


async def s_pattern(kind: str, n: int) -> None:
    async with _backend(kind) as (m, base):
        if kind == "aii_off":
            pattern = re.compile(rf"^http://{re.escape(URL(base).authority)}/items/\d+$")
        else:
            pattern = PATTERN_HOST_RE
        m.get(pattern, status=200, payload={"ok": True}, repeat=True)
        async with aiohttp.ClientSession() as session:
            for i in range(n):
                resp = await session.get(f"{base}/items/{i}")
                assert resp.status == 200


async def s_large(kind: str, n: int) -> None:
    async with _backend(kind) as (m, base):
        url = f"{base}/blob"
        m.get(url, status=200, body=LARGE_BODY, repeat=True)
        async with aiohttp.ClientSession() as session:
            for _ in range(n):
                resp = await session.get(url)
                data = await resp.read()
                assert len(data) == len(LARGE_BODY)


async def s_https(kind: str, n: int) -> None:
    # Only meaningful with the DNS/SSL patch; the driver skips aii_off.
    async with _backend(kind) as (m, _base):
        url = "https://api.example.com/secure"
        m.get(url, status=200, payload={"ok": True}, repeat=True)
        async with aiohttp.ClientSession() as session:
            for _ in range(n):
                resp = await session.get(url)
                assert resp.status == 200


async def s_many(kind: str, n: int) -> None:
    async with _backend(kind) as (m, base):
        for i in range(n):
            m.get(f"{base}/r/{i}", status=200, payload={"i": i})
        async with aiohttp.ClientSession() as session:
            for i in range(n):
                resp = await session.get(f"{base}/r/{i}")
                assert resp.status == 200


# (label, fn, key) — key indexes into SIZES_*.
SCENARIOS: list[tuple[str, Callable[..., Awaitable[None]], str]] = [
    ("lifecycle (CM + GET + assert)", s_lifecycle, "lifecycle"),
    ("fixture (1 CM, N x GET+assert+clear)", s_fixture, "fixture"),
    ("sequential GET", s_seq, "seq"),
    ("concurrent GET", s_conc, "conc"),
    ("POST + assert_called_with", s_post, "post"),
    ("regex pattern URL", s_pattern, "pattern"),
    ("large payload (1 MB)", s_large, "large"),
    ("HTTPS", s_https, "https"),
    ("many distinct mocked URLs", s_many, "many"),
]
SIZES_FULL = {
    "lifecycle": 20,
    "fixture": 50,
    "seq": 500,
    "conc": 500,
    "post": 200,
    "pattern": 200,
    "large": 20,
    "https": 20,
    "many": 500,
}
SIZES_QUICK = {
    "lifecycle": 5,
    "fixture": 10,
    "seq": 100,
    "conc": 100,
    "post": 30,
    "pattern": 30,
    "large": 5,
    "https": 5,
    "many": 50,
}


async def _measure(fn: Callable[[], Awaitable[None]], reps: int) -> float:
    timings = []
    for _ in range(reps):
        t0 = time.perf_counter()
        await fn()
        timings.append(time.perf_counter() - t0)
    return statistics.median(timings)


async def _warmup() -> None:
    for kind in ("aio", "aii_off", "aii_on"):
        async with _backend(kind) as (m, base):
            url = f"{base}/w"
            m.get(url, status=200)
            async with aiohttp.ClientSession() as session:
                await session.get(url)


def _fmt(secs: float | None, n: int) -> str:
    if secs is None:
        return "    N/A"
    us = secs / n * 1e6
    return f"{us / 1000:>7.2f} ms" if us >= 1000 else f"{us:>7.1f} us"


def _ratio(this: float | None, base: float | None) -> str:
    return f"{this / base:>5.1f}x" if (this and base) else "    --"


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true", help="Smaller N for a fast smoke run")
    p.add_argument("--reps", type=int, default=3, help="Repetitions per scenario (median reported)")
    p.add_argument("--scenario", default=None, help="Substring filter on scenario name")
    args = p.parse_args()

    sizes = SIZES_QUICK if args.quick else SIZES_FULL
    batch = 10 if args.quick else 50

    scenarios = SCENARIOS
    if args.scenario:
        needle = args.scenario.lower()
        scenarios = [s for s in SCENARIOS if needle in s[0].lower()]
        if not scenarios:
            raise SystemExit(f"No scenarios match: {args.scenario!r}")

    print("Warming up...")
    await _warmup()

    rows: list[tuple[str, int, float | None, float | None, float | None]] = []
    for label, fn, key in scenarios:
        n = sizes[key]
        name = f"{label} (batch={batch})" if key == "conc" else label
        print(f"  - {name} (N={n}, reps={args.reps})...", flush=True)
        extra = {"batch": batch} if key == "conc" else {}
        results: dict[str, float | None] = {}
        for k in ("aio", "aii_off", "aii_on"):
            if key == "https" and k == "aii_off":
                results[k] = None
                continue
            results[k] = await _measure(partial(fn, k, n, **extra), args.reps)
        rows.append((name, n, results["aio"], results["aii_off"], results["aii_on"]))

    bar = "=" * 100
    print("\n" + bar)
    print(
        f"{'Scenario':<35} {'N':>5}  {'aioresponses':>12}  {'aii(dns=F)':>12}  "
        f"{'aii(dns=T)':>12}  {'F/aio':>6}  {'T/aio':>6}"
    )
    print("-" * 100)
    for name, n, aio_t, off_t, on_t in rows:
        print(
            f"{name:<35} {n:>5}  {_fmt(aio_t, n):>12}  {_fmt(off_t, n):>12}  "
            f"{_fmt(on_t, n):>12}  {_ratio(off_t, aio_t):>6}  {_ratio(on_t, aio_t):>6}"
        )
    print(bar)
    print("Times are per-op (total / N), median of --reps runs. Ratios are vs aioresponses.")


if __name__ == "__main__":
    asyncio.run(main())
