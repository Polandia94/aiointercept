"""Tests for the auto-discovered pytest plugin (aiointercept.pytest_plugin)."""

import asyncio

import aiohttp

from aiointercept import CallbackResult, aiointercept


async def test_fixture_serves_and_records(aiointercept_mock: aiointercept) -> None:
    m = aiointercept_mock
    assert m.handlers == {}
    assert m.requests == {}
    url = f"{m.server_url}/hello"
    m.get(url, payload={"ok": True})
    async with aiohttp.ClientSession() as session:
        resp = await session.get(url)
        assert resp.status == 200
        assert await resp.json() == {"ok": True}
    m.assert_called_with(url, method="GET")


async def test_fixture_clears_between_tests(aiointercept_mock: aiointercept) -> None:
    m = aiointercept_mock
    assert m.handlers == {}, f"handlers leaked: {m.handlers!r}"
    assert m.requests == {}, f"requests leaked: {m.requests!r}"


async def test_async_callback_runs_on_test_loop(aiointercept_mock: aiointercept) -> None:
    m = aiointercept_mock
    test_loop = asyncio.get_running_loop()
    seen: list[asyncio.AbstractEventLoop] = []

    async def cb(_url: str, **_kwargs: object) -> CallbackResult:
        seen.append(asyncio.get_running_loop())
        return CallbackResult(payload={"ok": True})

    url = f"{m.server_url}/cb"
    m.get(url, callback=cb)
    async with aiohttp.ClientSession() as session:
        await session.get(url)
    assert seen == [test_loop]
