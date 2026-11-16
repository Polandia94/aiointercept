"""Tests for the auto-discovered pytest plugin (aiointercept.pytest_plugin)."""

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest

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


_INNER_INI = """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
"""


def test_options_fixture_reaches_constructor(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INNER_INI)
    pytester.makeconftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def aiointercept_options():
            return {"mock_external_urls": True}
        """
    )
    pytester.makepyfile(
        """
        import aiohttp

        async def test_external_url_is_mocked(aiointercept_mock):
            aiointercept_mock.get("http://example.invalid/x", payload={"ok": True})
            async with aiohttp.ClientSession() as session:
                resp = await session.get("http://example.invalid/x")
                assert await resp.json() == {"ok": True}
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_missing_pytest_asyncio_explains_itself(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    shadow = Path(pytester.path) / "shadow"
    shadow.mkdir()
    (shadow / "pytest_asyncio.py").write_text("raise ImportError('simulated: pytest-asyncio is not installed')\n")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(shadow), os.environ.get("PYTHONPATH")])))
    pytester.makepyfile(
        """
        def test_needs_fixture(aiointercept_mock):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:asyncio")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*pytest-asyncio 0.24*aiointercept[[]pytest-asyncio[]]*"])
