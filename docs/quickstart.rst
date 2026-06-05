Quickstart
==========

There are four ways to drive the mock lifecycle. Pick whichever fits your test
framework.

Context manager
---------------

.. code-block:: python

    import aiohttp
    from aiointercept import aiointercept

    async def test_get_user():
        async with aiointercept() as m:
            m.get(f"{m.server_url}/user/1", payload={"id": 1, "name": "Alice"})

            async with aiohttp.ClientSession() as session:
                resp = await session.get(f"{m.server_url}/user/1")
                assert resp.status == 200
                assert await resp.json() == {"id": 1, "name": "Alice"}

Decorator
---------

The :class:`~aiointercept.aiointercept` instance is injected as the last
positional argument, or under the name given by ``param``:

.. code-block:: python

    from aiointercept import aiointercept

    @aiointercept()
    async def test_create_post(m):
        m.post(f"{m.server_url}/posts", status=201, payload={"id": 42})
        ...

    @aiointercept(param="mock")
    async def test_named(mock):
        mock.get(f"{mock.server_url}/feed", payload=[])
        ...

pytest fixture
--------------

.. code-block:: python

    import pytest_asyncio
    from aiointercept import aiointercept

    @pytest_asyncio.fixture
    async def mock_http():
        async with aiointercept() as m:
            yield m

    async def test_something(mock_http):
        mock_http.get(f"{mock_http.server_url}/items", payload=[{"id": 1}])
        ...

.. note::

    ``@pytest_asyncio.fixture`` works in all pytest-asyncio modes. If you use
    ``asyncio_mode = "auto"`` in ``pyproject.toml``, a plain ``@pytest.fixture``
    works too.

Manual ``start()`` / ``stop()``
-------------------------------

For frameworks with their own setup/teardown hooks (e.g.
:class:`unittest.IsolatedAsyncioTestCase`) you can drive the lifecycle
explicitly:

.. code-block:: python

    import unittest
    from aiointercept import aiointercept

    class TestApi(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            self.helper = aiointercept()
            await self.helper.start()

        async def asyncTearDown(self):
            await self.helper.stop()

        async def test_user(self):
            self.helper.get(f"{self.helper.server_url}/user/1", payload={"id": 1})
            ...

Sharing the server across tests
-------------------------------

Each ``async with aiointercept()`` block starts a fresh :mod:`aiohttp.web` test
server. The bundled pytest plugin (auto-discovered, requires ``pytest-asyncio``)
amortizes that with a session-scoped server and a function-scoped wrapper that
calls :meth:`~aiointercept.aiointercept.clear` between tests.

.. code-block:: python

    async def test_get_user(aiointercept_mock):
        m = aiointercept_mock
        m.get(f"{m.server_url}/user/1", payload={"id": 1, "name": "Alice"})

        async with aiohttp.ClientSession() as session:
            resp = await session.get(f"{m.server_url}/user/1")
            assert resp.status == 200
        m.assert_called_with(f"{m.server_url}/user/1", method="GET")

Fixtures: ``aiointercept_server`` (session) and ``aiointercept_mock``
(function — use this in tests). Override ``aiointercept_server`` in your
``conftest.py`` to customize.

.. note::

    The plugin defaults to ``mock_external_urls=False``. To intercept URLs for
    the whole session, override ``aiointercept_server`` and pass
    ``mock_external_urls=True`` — note that the DNS/SSL patches are installed at
    the class level and stay live for every aiohttp call in the test process
    (this blocks real network calls).
