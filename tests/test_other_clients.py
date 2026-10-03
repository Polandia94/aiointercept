"""With ``mock_external_urls=False`` the mock is a plain HTTP server at
``m.server_url``, so any HTTP client can talk to it, not just aiohttp."""

import httpx
import requests

from aiointercept import CallbackResult, aiointercept


async def test_requests_client():
    async with aiointercept(mock_external_urls=False) as m:
        url = f"{m.server_url}/users"
        m.post(url, status=201, payload={"id": 1, "name": "alice"}, headers={"X-Mock": "yes"})

        # The server runs on its own thread, so a blocking call does not deadlock.
        resp = requests.post(url, json={"name": "alice"}, headers={"X-Client": "requests"}, timeout=5)  # noqa: ASYNC210

        assert resp.status_code == 201
        assert resp.json() == {"id": 1, "name": "alice"}
        assert resp.headers["X-Mock"] == "yes"
        m.assert_called_once_with(url, method="POST", json={"name": "alice"}, headers={"X-Client": "requests"})


async def test_httpx_sync_client():
    async with aiointercept(mock_external_urls=False) as m:
        url = f"{m.server_url}/users"
        m.post(url, status=201, payload={"id": 1, "name": "alice"}, headers={"X-Mock": "yes"})

        with httpx.Client(timeout=5) as client:
            resp = client.post(url, json={"name": "alice"}, headers={"X-Client": "httpx-sync"})  # noqa: ASYNC212

        assert resp.status_code == 201
        assert resp.json() == {"id": 1, "name": "alice"}
        assert resp.headers["X-Mock"] == "yes"
        m.assert_called_once_with(url, method="POST", json={"name": "alice"}, headers={"X-Client": "httpx-sync"})


async def test_httpx_async_client():
    async with aiointercept(mock_external_urls=False) as m:
        url = f"{m.server_url}/users"
        m.post(url, status=201, payload={"id": 1, "name": "alice"}, headers={"X-Mock": "yes"})

        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={"name": "alice"}, headers={"X-Client": "httpx-async"})

        assert resp.status_code == 201
        assert resp.json() == {"id": 1, "name": "alice"}
        assert resp.headers["X-Mock"] == "yes"
        m.assert_called_once_with(url, method="POST", json={"name": "alice"}, headers={"X-Client": "httpx-async"})


async def test_httpx_async_client_callback_sees_request():
    seen = {}

    def callback(url, **kwargs):
        seen["json"] = kwargs["json"]
        return CallbackResult(status=200, payload={"echo": kwargs["json"]})

    async with aiointercept(mock_external_urls=False) as m:
        url = f"{m.server_url}/echo"
        m.post(url, callback=callback)

        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={"a": 1})

        assert resp.status_code == 200
        assert resp.json() == {"echo": {"a": 1}}
        assert seen["json"] == {"a": 1}
