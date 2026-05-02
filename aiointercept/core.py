import socket
import threading
from functools import wraps
from re import Pattern
import typing
import json as json_module
import inspect
import warnings
import gc
from urllib.parse import parse_qs, urlencode
import logging

import aiohttp
from aiohttp import ClientRequest, ClientResponse, web, hdrs
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.connector import SSLContext, TCPConnector
from aiohttp.resolver import ThreadedResolver, AsyncResolver
from aiohttp.test_utils import TestServer
from aiohttp.web_request import Request
from yarl import URL
from typing import Any, Awaitable, Callable, Mapping, Sequence, Type
from .compat import merge_params, normalize_url

logger = logging.getLogger(__name__)

_PROXY_REQ_DROP = frozenset(("host", "transfer-encoding", "x-aiointercept-orig-scheme"))
_PROXY_RESP_DROP = frozenset(("transfer-encoding", "content-encoding"))

# Module-level state for class-level patches shared across concurrent instances.
# Only the first entering instance installs the patches; the last exiting one removes them.
_patch_lock = threading.Lock()
_patch_refcount: int = 0
_real_threaded_resolve: Any = None
_real_async_resolve: Any = None
_real_ssl_context: Any = None
_active_instances: "list[aiointercept]" = []


def _make_resolve_result(
    host: str, inst: "aiointercept", family: "socket.AddressFamily"
) -> "ResolveResult":
    return ResolveResult(
        hostname=host,
        host=inst.server_host,
        port=inst.server_port,
        family=family,
        proto=0,
        flags=0,
    )


def _pick_real_resolver(resolver_self: "AbstractResolver") -> Any:
    return (
        _real_threaded_resolve
        if isinstance(resolver_self, ThreadedResolver)
        else _real_async_resolve
    )


async def _shared_resolve(
    resolver_self: "AbstractResolver",
    host: str,
    port: int = 0,
    family: "socket.AddressFamily" = socket.AF_INET,
) -> "list[ResolveResult]":
    with _patch_lock:
        instances = list(reversed(_active_instances))

    for inst in instances:
        if host in inst._host_list or inst._patterns_list:
            return [_make_resolve_result(host, inst, family)]

    for inst in instances:
        if host in inst._passthrough_hosts or inst.passthrough_unmatched:
            return await _pick_real_resolver(resolver_self)(
                resolver_self, host, port, family
            )

    # No instance claims this host and none allow passthrough — redirect to
    # the innermost instance's server so the client gets a clear connection error.
    if instances:
        return [_make_resolve_result(host, instances[0], family)]

    return await _pick_real_resolver(resolver_self)(resolver_self, host, port, family)


def _shared_ssl_context(
    connector_self: "TCPConnector", req: "ClientRequest"
) -> "SSLContext | None":
    with _patch_lock:
        instances = list(reversed(_active_instances))

    host = req.url.raw_host
    url_str = str(req.url)

    for inst in instances:
        if host in inst._host_list or inst._match_pattern(url_str):
            if req.url.scheme == "https":
                req.headers["X-Aiointercept-Orig-Scheme"] = "https"
            return None

    for inst in instances:
        if inst._patterns_list and (
            inst.passthrough_unmatched or host in inst._passthrough_hosts
        ):
            if req.url.scheme == "https":
                req.headers["X-Aiointercept-Orig-Scheme"] = "https"
            return None

    return _real_ssl_context(connector_self, req)  # type: ignore[misc]


class CallbackResult:
    """Result object returned by a callback.

    Args:
        method: HTTP method (default GET; not used by the server handler).
        status: HTTP response status code.
        body: Raw response body as str or bytes.
        content_type: ``Content-Type`` header value.
        payload: Response body as a dict; serialized to JSON automatically.
        headers: Additional response headers.
        response_class: Ignored (present for aioresponses API compatibility).
        reason: HTTP reason phrase.
    """

    def __init__(
        self,
        method: str = hdrs.METH_GET,
        status: int = 200,
        body: str | bytes = "",
        content_type: str = "application/json",
        payload: Any = None,
        headers: Mapping[str, str] | None = None,
        response_class: Type[ClientResponse] | None = None,
        reason: str | None = None,
    ):
        self.method = method
        self.status = status
        self.body = body
        self.content_type = content_type
        self.payload = payload
        self.headers = headers
        self.response_class = response_class
        self.reason = reason


handler_type = Callable[[web.Request], Awaitable[web.StreamResponse]]


class aiointercept:
    """
    Mock aiohttp requests by redirecting DNS to a local aiohttp.web test server.
    """

    def __init__(
        self,
        mock_external_urls: bool,
        passthrough: Sequence[str] | None = None,
        passthrough_unmatched: bool = False,
        param: str | None = None,
        **kwargs: dict[str, Any],
    ) -> None:
        if kwargs:
            warnings.warn(
                "Passing extra parameters to aiointercept via kwargs is deprecated and will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )

        self._passthrough_urls = passthrough or []
        self._passthrough_hosts: list[str] = []
        self._mock_external_urls = mock_external_urls

        if mock_external_urls:
            for p in self._passthrough_urls:
                host = URL(p).host
                self._passthrough_hosts.append(host if host else p)

        self.param = param
        self.passthrough_unmatched = passthrough_unmatched

        self._host_list: list[str] = []
        self._https_hosts: set[str] = set()
        self._patterns_list: list[Pattern[str]] = []

        # handler are (path, method) → handler or list of handlers (if repeat != True)
        self.handlers: dict[tuple[str, str], handler_type | list[handler_type]] = {}
        # patterns_handler are (pattern, method) → handler or list of handlers (if repeat != True)
        self.patterns_handler: dict[
            tuple[Pattern[str], str], handler_type | list[handler_type]
        ] = {}

        # recorded requests: {(METHOD, URL): [web.Request, ...]}
        self.requests: dict[tuple[str, URL], list[Request]] = {}

        self.server: TestServer | None = None
        self._bypass_session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "aiointercept":
        app = web.Application()
        # we add every route to the app.
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self.server = TestServer(app)
        await self.server.start_server()

        assert isinstance(self.server.host, str) and isinstance(self.server.port, int)  # pyright: ignore[reportUnknownMemberType]
        self.server_host = self.server.host
        self.server_port = self.server.port
        self.server_url = f"http://{self.server_host}:{self.server.port}"

        if self._mock_external_urls:
            global \
                _patch_refcount, \
                _real_threaded_resolve, \
                _real_async_resolve, \
                _real_ssl_context
            with _patch_lock:
                _active_instances.append(self)
                if _patch_refcount == 0:
                    _real_threaded_resolve = ThreadedResolver.resolve
                    _real_async_resolve = AsyncResolver.resolve
                    _real_ssl_context = TCPConnector._get_ssl_context  # pyright: ignore[reportPrivateUsage]
                    ThreadedResolver.resolve = _shared_resolve  # type: ignore
                    AsyncResolver.resolve = _shared_resolve  # type: ignore
                    TCPConnector._get_ssl_context = _shared_ssl_context  # type: ignore
                _patch_refcount += 1
            self._clear_all_connector_caches()
            self._bypass_session = self._make_bypass_session()

        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self._mock_external_urls:
            global \
                _patch_refcount, \
                _real_threaded_resolve, \
                _real_async_resolve, \
                _real_ssl_context
            with _patch_lock:
                _active_instances.remove(self)
                _patch_refcount -= 1
                if _patch_refcount == 0:
                    ThreadedResolver.resolve = _real_threaded_resolve  # type: ignore[method-assign]
                    AsyncResolver.resolve = _real_async_resolve  # type: ignore[method-assign]
                    TCPConnector._get_ssl_context = _real_ssl_context  # type: ignore[method-assign,reportPrivateUsage]
                    _real_threaded_resolve = None
                    _real_async_resolve = None
                    _real_ssl_context = None
        if self._bypass_session:
            await self._bypass_session.close()
            self._bypass_session = None
        if self.server:
            await self.server.close()
            self.server = None
        self.clear()

    # Decorator support
    def __call__(
        self, f: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        @wraps(f)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with self as m:
                if self.param:
                    kwargs[self.param] = m
                else:
                    if args and hasattr(args[0], f.__name__):
                        args = (args[0], m) + args[1:]
                    else:
                        args = args + (m,)
                return await f(*args, **kwargs)

        return wrapper

    def _make_bypass_session(self) -> aiohttp.ClientSession:
        _orig_resolve = _real_threaded_resolve
        _orig_ssl_ctx = _real_ssl_context

        class _BypassResolver(ThreadedResolver):
            async def resolve(
                self,
                host: str,
                port: int = 0,
                family: socket.AddressFamily = socket.AF_INET,
            ) -> list[ResolveResult]:
                return await _orig_resolve(self, host, port, family)

        class _BypassConnector(aiohttp.TCPConnector):
            def _get_ssl_context(self, req: ClientRequest) -> SSLContext | None:
                return _orig_ssl_ctx(self, req)  # pyright: ignore[reportPrivateUsage]

        return aiohttp.ClientSession(
            connector=_BypassConnector(resolver=_BypassResolver())
        )

    def _match_pattern(self, url: str) -> bool:
        return any(p.match(url) for p in self._patterns_list)

    @staticmethod
    def _clear_all_connector_caches() -> None:
        """
        Walk every TCPConnector referenced by a live ClientSession and clear
        its DNS cache.  This ensures pre-patch resolutions are not reused.
        """
        for obj in gc.get_objects():
            if not issubclass(type(obj), aiohttp.TCPConnector):
                continue
            try:
                obj.clear_dns_cache()
            except Exception:
                pass

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        url = normalize_url(request.url)
        req_host = request.headers.get("Host", "")
        if request.headers.get("X-Aiointercept-Orig-Scheme") == "https":
            self._https_hosts.add(req_host)
        if req_host in self._https_hosts:
            url = url.with_scheme("https")

        key = (request.method.upper(), url)
        self.requests.setdefault(key, [])
        request._captured_body = await request.read() if request.can_read_body else b""
        try:
            json = (
                json_module.loads(request._captured_body)  # type: ignore[attr-defined]
                if request._captured_body  # type: ignore[attr-defined]
                else None
            )
        except Exception:
            json = None
        # this kwargs will be removed, should be deprecated in the future
        request.kwargs = {
            "headers": request.headers,
            # Use getall so duplicate keys (?a=1&a=2) aren't collapsed to one value.
            "query": {k: request.query.getall(k) for k in dict.fromkeys(request.query)},
            "json": json,
        }
        # Read body eagerly before the handler runs, because aiohttp sets
        # PayloadAccessError on the stream once the response cycle completes.
        self.requests[key].append(request)
        url_str = str(url)
        selected_handler = self.handlers.get((url_str, request.method))
        if isinstance(selected_handler, list):
            if not selected_handler:
                handler: handler_type | None = None
            else:
                handler = typing.cast(handler_type, selected_handler.pop(0))

        else:
            handler = selected_handler
        original_host = request.headers.get("Host", request.url.host)
        if handler is None:
            original_urls = [
                f"https://{original_host}{request.path_qs}",
                f"http://{original_host}{request.path_qs}",
            ]
            for (pattern, method), pattern_handler in self.patterns_handler.items():
                if (
                    any(pattern.match(u) for u in original_urls)
                    and method == request.method
                ):
                    if isinstance(pattern_handler, list):
                        handler = pattern_handler[0]
                        remaining = pattern_handler[1:]
                        if remaining:
                            self.patterns_handler[pattern, request.method] = remaining
                        else:
                            del self.patterns_handler[pattern, request.method]
                    else:
                        handler = pattern_handler
                    break

        if handler is None:
            if (
                self._mock_external_urls
                and self._patterns_list
                and self.passthrough_unmatched
            ):
                scheme = request.headers.get("X-Aiointercept-Orig-Scheme") or (
                    "https" if request.secure else "http"
                )
                real_url = f"{scheme}://{original_host}{request.path_qs}"
                session = self._bypass_session or self._make_bypass_session()
                async with session.request(
                    method=request.method,
                    url=real_url,
                    headers={
                        k: v
                        for k, v in request.headers.items()
                        if k.lower() not in _PROXY_REQ_DROP
                    },
                    data=getattr(request, "_captured_body", None) or None,
                    allow_redirects=True,
                    ssl=True,
                ) as real_resp:
                    body = await real_resp.read()
                    return web.Response(
                        status=real_resp.status,
                        headers={
                            k: v
                            for k, v in real_resp.headers.items()
                            if k.lower() not in _PROXY_RESP_DROP
                        },
                        body=body,
                    )
            # this should raise ClientConnectionError on the other side
            if request.transport:
                request.transport.close()
            return web.Response(
                status=502, text="No handler registered for this request."
            )
        return await handler(request)

    def add(
        self,
        url: URL | str | Pattern[str],
        method: str = hdrs.METH_GET,
        status: int = 200,
        body: str | bytes = b"",
        json: Any = None,
        payload: Any = None,
        headers: Mapping[str, str] | None = None,
        repeat: bool | int = False,
        content_type: str | None = None,
        callback: Callable[..., CallbackResult | Awaitable[CallbackResult]]
        | None = None,
        reason: str | None = None,
        exception: Exception | bool | None = None,
        **kwargs,
    ) -> None:
        """Register a mock handler for *url* and *method*.

        Args:
            url: Target URL as str, :class:`~yarl.URL`, or compiled
                :class:`re.Pattern`.
            method: HTTP method (case-insensitive, default ``GET``).
            status: Response status code.
            body: Raw response body (str is UTF-8 encoded; default empty bytes).
            json: Response body as a JSON-serialisable object (overrides *body*).
            payload: Alias for *json*.
            headers: Additional response headers.
            repeat: ``True`` to respond indefinitely; integer N to respond N
                times; ``False`` or ``0`` to respond once (default).
            content_type: Override the ``Content-Type`` response header.
            callback: Callable ``(url, *, headers, query, json) → CallbackResult``
                (sync or async).  Takes precedence over *body* / *json* / *status*.
            reason: HTTP reason phrase.
            exception: Any truthy value registers a handler that closes the
                connection, causing :class:`~aiohttp.ClientConnectionError` on the
                client.  Passing a specific exception instance logs a warning;
                pass ``exception=True`` to suppress it.
        """
        if exception:
            if exception is not True:
                logger.warning(
                    "aiointercept only raise ClientConnectionError, pass exception=True instead of an specific exception"
                )
            # if there is an excpetion, dont add handler, will return a clientDisconnectionError
            # add some deprecation or similar
            return
        method = method.upper()
        if isinstance(url, str):
            url = URL(url)

        if isinstance(url, Pattern):
            self._patterns_list.append(url)

        assert self.server is not None, (
            "Server not started — use `async with aiointercept() as m:` first."
        )
        if isinstance(url, URL):
            host = url.host
            assert host, f"Cannot extract host from {url!r}"

            # Map this host → our test server
            self._host_list.append(host)

        if json is not None:
            body = json_module.dumps(json).encode()
        elif payload is not None:
            body = json_module.dumps(payload).encode()
        elif isinstance(body, str):
            body = body.encode()

        resp_headers = dict(headers or {})
        if not content_type and "Content-Type" not in resp_headers:
            content_type = "application/json"

        async def handler(request: web.Request) -> web.Response:
            if callable(callback):
                if inspect.iscoroutinefunction(callback):
                    result = await callback(url, **request.kwargs)  # type: ignore[attr-defined]
                else:
                    result = callback(url, **request.kwargs)  # type: ignore[attr-defined]
                _status = result.status
                _body = result.body
                _headers = result.headers or {}
                if result.payload is not None:
                    _body = json_module.dumps(result.payload).encode()
                _content_type = result.content_type
                _reason = result.reason
            else:
                _status = status
                _body = body
                _headers = headers
                _content_type = content_type
                _reason = reason

            return web.Response(
                status=_status,
                body=_body,
                headers=_headers,
                reason=_reason,
                content_type=_content_type,
            )

        if repeat is True:
            if isinstance(url, Pattern):
                self.patterns_handler[url, method] = handler
                return
            handler_url = str(normalize_url(url))
            self.handlers[handler_url, method] = handler
        else:
            if repeat is False or repeat == 0:
                repeat = 1
            if repeat < 1:
                raise ValueError("repeat must be at least 1")
            handlers: list[handler_type] = [handler] * repeat
            if isinstance(url, Pattern):
                if (url, method) in self.patterns_handler:
                    list_pattern_handler = self.patterns_handler[(url, method)]
                    if isinstance(list_pattern_handler, list):
                        list_pattern_handler = typing.cast(
                            list[handler_type], list_pattern_handler
                        )
                        list_pattern_handler += handlers
                    else:
                        raise ValueError(
                            f"Existing handler for pattern {url} {method} has repeat=True, cannot add more handlers to it."
                        )

                else:
                    self.patterns_handler[url, method] = handlers
                return
            handler_url = str(normalize_url(url))
            if (handler_url, method) in self.handlers:
                handlers_list = self.handlers[(handler_url, method)]
                if isinstance(handlers_list, list):
                    handlers_list = typing.cast(list[handler_type], handlers_list)
                    handlers_list += handlers
                else:
                    raise ValueError(
                        f"Existing handler for {handler_url} {method} has repeat=True, cannot add more handlers to it."
                    )
            else:
                self.handlers[handler_url, method] = handlers

    def get(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock GET handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_GET, **kwargs)

    def post(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock POST handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_POST, **kwargs)

    def put(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock PUT handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_PUT, **kwargs)

    def patch(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock PATCH handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_PATCH, **kwargs)

    def delete(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock DELETE handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_DELETE, **kwargs)

    def head(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock HEAD handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_HEAD, **kwargs)

    def options(self, url: "URL | str | Pattern[str]", **kwargs: Any) -> None:
        """Register a mock OPTIONS handler. See :meth:`add` for all keyword arguments."""
        self.add(url, method=hdrs.METH_OPTIONS, **kwargs)

    def clear(self) -> None:
        """Clear all recorded requests and registered handlers."""
        self.requests.clear()
        self.handlers.clear()
        self.patterns_handler.clear()
        self._host_list.clear()
        self._patterns_list.clear()

    def assert_called(self) -> None:
        """Assert that at least one request was made."""
        if not self.requests:
            raise AssertionError("Expected at least one call, got none.")

    def assert_not_called(self) -> None:
        """Assert that no requests were made."""
        if self.requests:
            raise AssertionError(
                f"Expected no calls, got {sum(len(v) for v in self.requests.values())}."
            )

    def assert_called_once(self) -> None:
        """Assert that exactly one request was made across all URLs."""
        count = sum(len(v) for v in self.requests.values())
        if count != 1:
            raise AssertionError(f"Expected exactly 1 call, got {count}.")

    def assert_any_call(
        self,
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: Mapping[str, str] | None = None,
    ) -> None:
        """Assert that *url* was called at least once with the given *method*."""
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")

    def assert_called_with(
        self,
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: typing.Mapping[str, str] | None = None,
        data: str | bytes | typing.Mapping[str, Any] | None = None,
        json: Any = None,
        headers: typing.Mapping[str, str] | None = None,
        strict_headers: bool = False,
    ) -> None:
        """Assert that the most recent call to *url* matched the given arguments.

        Args:
            url: Expected URL (str or :class:`~yarl.URL`).
            method: Expected HTTP method (default ``GET``).
            params: Query string params merged into *url* before lookup.
            data: Expected request body — bytes, str, or a dict (form-encoded via
                ``application/x-www-form-urlencoded``).
            json: Expected request body as a JSON-serialisable object.
            headers: Expected request headers.  By default only the headers listed
                here are checked; auto-added aiohttp headers are ignored.  Set
                *strict_headers* to compare the full header map.
            strict_headers: When ``True``, the complete set of actual request
                headers must match *headers* exactly.  Use
                :data:`unittest.mock.ANY` as a value to accept any value for a
                specific key (e.g. ``Content-Length``).
        """
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")
        request = self.requests[key][-1]  # most recent call
        actual_body = getattr(request, "_captured_body", b"")
        if json is not None:
            # aiohttp sends json= as JSON-encoded bytes with application/json
            expected_body = json_module.dumps(json).encode()
            assert actual_body == expected_body, (
                f"Expected JSON body {json!r}, got {actual_body!r}"
            )
        elif data is not None:
            if not isinstance(data, (str, bytes)):
                actual_ct = request.headers.get("Content-Type", "")
                if actual_ct and "application/x-www-form-urlencoded" not in actual_ct:
                    raise AssertionError(
                        f"data=dict assertion requires Content-Type: "
                        f"application/x-www-form-urlencoded, got {actual_ct!r}. "
                        f"Use json= for JSON bodies."
                    )
                actual_qs = parse_qs(actual_body.decode(errors="replace"))
                expected_qs = parse_qs(urlencode(sorted(data.items())))
                assert actual_qs == expected_qs, (
                    f"Expected body {data!r} (form encoded), got {actual_body!r}"
                )
            else:
                if isinstance(data, str):
                    expected_body = data.encode()
                else:
                    expected_body = data
                assert actual_body == expected_body, (
                    f"Expected body {expected_body!r}, got {actual_body!r}"
                )
        if strict_headers:
            actual_headers = dict(request.headers)
            actual_headers.pop("x-aiointercept-orig-scheme", None)
            expected_headers = headers or {}
            assert expected_headers == actual_headers, (
                f"Expected headers {expected_headers!r}, got {actual_headers!r}"
            )
        elif headers:
            actual_headers = dict(request.headers)
            for k, v in headers.items():
                assert actual_headers.get(k) == v, (
                    f"Header {k!r}: expected {v!r}, got {actual_headers.get(k)!r}"
                )

    def assert_called_once_with(
        self,
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: typing.Mapping[str, str] | None = None,
        data: str | bytes | typing.Mapping[str, Any] | None = None,
        json: Any = None,
        headers: typing.Mapping[str, str] | None = None,
        strict_headers: bool = False,
    ) -> None:
        """Assert that exactly one request was made and it matched the given arguments."""
        self.assert_called_once()
        self.assert_called_with(
            url, method, params, data, json, headers, strict_headers
        )
