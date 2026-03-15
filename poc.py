import socket
from aiohttp import web
from aiohttp.resolver import DefaultResolver
from aiohttp.test_utils import TestServer
from yarl import URL
from unittest.mock import patch
import aiohttp
from multidict import MultiDict


class FakeResolver(DefaultResolver):
    def __init__(self, *args, **kwargs):
        self._mapping = {}
        super().__init__(*args, **kwargs)

    def add_mapping(self, host, ip, port):
        self._mapping[host] = (ip, port)

    async def resolve(self, host, port=0, family=socket.AF_INET):
        # Redirect host and port if it matches our mapping
        target = self._mapping.get(host)
        if target:
            target_host, target_port = target
            return [
                {
                    "hostname": host,
                    "host": target_host,
                    "port": target_port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        return await super().resolve(host, port, family)


def normalize_url(url: URL | str) -> URL:
    """Normalize url to make comparisons."""
    url = URL(url)
    return url.with_query(sorted(url.query.items()))


def merge_params(url: URL | str, params: dict | None = None) -> URL:
    url = URL(url)
    if params:
        query_params = MultiDict(url.query)
        query_params.extend(url.with_query(params).query)
        return url.with_query(query_params)
    return url


class aioresponses_2:
    def __init__(self):
        self.resolver = FakeResolver()
        self.handlers = {}

    async def _dispatch(self, request):
        handler = self.handlers.get(request.path)
        if handler:
            return await handler(request)
        return web.Response(status=404, text="Not Found")

    async def __aenter__(self, **kwargs):
        self.requests = {}
        app = web.Application()
        # Add a catch-all route that can handle dynamic paths
        app.router.add_route("*", "/{tail:.*}", self._dispatch)

        self.server = TestServer(app, **kwargs)
        await self.server.start_server()
        self.app = app

        # Patch TCPConnector to use our resolver by default
        original_init = aiohttp.TCPConnector.__init__

        def patched_init(self_conn, *args, **kwargs):
            if "resolver" not in kwargs:
                kwargs["resolver"] = self.resolver
            return original_init(self_conn, *args, **kwargs)

        self._patcher = patch("aiohttp.connector.TCPConnector.__init__", patched_init)
        self._patcher.start()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._patcher.stop()
        await self.server.close()

    def get(self, url: URL | str, status=200, body="OK"):
        async def handler(request):
            key = (request.method.upper(), request.url)
            self.requests.setdefault(key, [])
            self.requests[key].append(request)
            return web.Response(status=status, text=body)

        if isinstance(url, str):
            url = URL(url)

        # we map the host of the url to the site host AND port
        self.resolver.add_mapping(url.host, self.server.host, self.server.port)

        # we add the handler to our dynamic map
        self.handlers[url.path] = handler

    def assert_called_with(
        self, url: URL | str, method: str = "GET", params: dict | None = None
    ):
        """assert that the last call was made with the specified arguments.

        Raises an AssertionError if the args and keyword args passed in are
        different to the last call to the mock."""
        url = normalize_url(merge_params(url, params))
        method = method.upper()
        key = (method, url)
        try:
            expected = self.requests[key][-1]
        except KeyError:
            raise AssertionError(f"No calls to {method} {url}")

        # we need to create a request object to match the expected one
        assert isinstance(expected, web.Request)
        assert expected.method == method
        assert expected.url == url
