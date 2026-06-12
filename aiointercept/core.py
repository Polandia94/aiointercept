import asyncio
import difflib
import inspect
import json as json_module
import logging
import pprint
import socket
import threading
import typing
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from functools import wraps
from re import Pattern
from typing import Any, TypedDict, cast
from unittest import mock
from urllib.parse import parse_qs, urlencode

import aiohttp
from aiohttp import ClientRequest, ClientResponse, hdrs, web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.connector import SSLContext, TCPConnector
from aiohttp.resolver import AsyncResolver, ThreadedResolver
from aiohttp.test_utils import TestServer
from yarl import URL

from .compat import merge_params, normalize_url

# Bounds for the diff body. ``ndiff`` is O(n²) in time and memory, so we clip
# both the per-line width (a raw body is a single very long line) and the line
# count *before* diffing to keep failures fast and readable on large values.
_MAX_DIFF_LINE_LEN = 200
_MAX_DIFF_LINES = 60


def _pformat(value: Any) -> list[str]:
    """Pretty-print *value* into a list of lines for diffing.

    ``bytes`` are decoded best-effort so the diff stays human-readable; other
    objects go through :func:`pprint.pformat` so nested dicts/lists align. Each
    line is clipped to :data:`_MAX_DIFF_LINE_LEN` so a huge single-line body
    can't push :func:`difflib.ndiff` into quadratic blow-up.
    """
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        text = pprint.pformat(value, width=88, sort_dicts=True)
    lines = text.splitlines() or [""]
    return [
        line if len(line) <= _MAX_DIFF_LINE_LEN else line[:_MAX_DIFF_LINE_LEN] + " …(line truncated)" for line in lines
    ]


def _oneline(value: Any, limit: int = 120) -> str:
    """Render *value* on a single line for the assertion's summary line."""
    text = " ".join("\n".join(_pformat(value)).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _diff(label: str, expected: Any, actual: Any) -> str:
    """Build an assertion message diffing *expected* vs *actual*.

    The first line is a compact ``expected ... got ...`` summary so it reads well
    in pytest's one-line failure list; the detailed :func:`difflib.ndiff` follows
    (``-`` marks expected, ``+`` marks actual, ``?`` points at differing chars).
    """
    summary = f"{label}: expected {_oneline(expected)}, got {_oneline(actual)}"
    expected_lines = _pformat(expected)
    actual_lines = _pformat(actual)
    clipped = max(len(expected_lines), len(actual_lines)) > _MAX_DIFF_LINES
    diff = difflib.ndiff(expected_lines[:_MAX_DIFF_LINES], actual_lines[:_MAX_DIFF_LINES])
    body = "\n".join(line.rstrip("\n") for line in diff)
    if not body:
        body = "(values render identically; check types)"
    if clipped:
        body += "\n... (diff truncated; values too large to show in full)"
    return f"{summary}\n{body}"


class AiointerceptRequestKwargs(TypedDict):
    headers: Mapping[str, str]
    query: Mapping[str, Sequence[str]]
    json: Any | None
    data: bytes | None


class AiointerceptRequest(web.Request):
    """A subclass of :class:`aiohttp.web.Request` captured by the mock server.

    Instances are the live request objects the test server received, augmented
    with the fields below so callbacks and assertions can inspect the body that
    was already consumed off the wire. They are stored in
    :attr:`aiointercept.requests` and passed to callbacks registered via
    :meth:`aiointercept.add`.

    Attributes:
        captured_body: The raw request body as bytes, read before dispatch.
        kwargs: A mapping with the parsed ``headers``, ``query`` (``dict`` of
            key → list of values), ``data`` (raw body or ``None`` if no body),
            `and ``json`` (decoded body, or ``None``) — the keyword arguments
            a callback receives.
        canonical_url: The normalized request URL with the original scheme
            restored (e.g. ``https://`` even though the test server received the
            request over plain HTTP). This is the URL passed to callbacks and
            used as the :attr:`aiointercept.requests` key.
    """

    captured_body: bytes
    kwargs: AiointerceptRequestKwargs
    canonical_url: URL

    @classmethod
    def upgrade(
        cls,
        request: web.Request,
        captured_body: bytes | None,
        kwargs: "AiointerceptRequestKwargs",
        canonical_url: URL,
    ) -> "AiointerceptRequest":
        request.__class__ = cls
        request.captured_body = captured_body  # type: ignore[attr-defined]
        request.kwargs = kwargs  # type: ignore[attr-defined]
        request.canonical_url = canonical_url  # type: ignore[attr-defined]
        return cast("AiointerceptRequest", request)


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
_real_resolve_host: Any = None
_real_get: Any = None
_active_instances: "list[aiointercept]" = []


async def _shared_resolve_host(
    connector_self: "TCPConnector",
    host: str,
    port: int,
    traces: Any = None,
) -> "list[ResolveResult]":
    # While mocking is active, never serve a resolution that was cached before
    # the resolver patch was installed — drop this connector's cached entry so
    # the lookup always falls through to the patched resolver. This replaces
    # the need to enumerate every live TCPConnector and clear its DNS cache.
    # Best-effort: a failing clear must not break resolution.
    try:
        connector_self.clear_dns_cache(host, port)
    except Exception:
        logger.debug("clear_dns_cache failed for %s:%s", host, port, exc_info=True)
    return await _real_resolve_host(connector_self, host, port, traces)


def _make_resolve_result(host: str, inst: "aiointercept", family: "socket.AddressFamily") -> "ResolveResult":
    return ResolveResult(
        hostname=host,
        host=inst.server_host,
        port=inst.server_port,
        family=family,
        proto=0,
        flags=0,
    )


def _pick_real_resolver(resolver_self: "AbstractResolver") -> Any:
    return _real_threaded_resolve if isinstance(resolver_self, ThreadedResolver) else _real_async_resolve


def _resolution_target(host: str) -> "aiointercept | None":
    """Return the mock instance whose server should serve ``host``, or ``None``
    if the host should be resolved for real (passthrough, or no active mock)."""
    with _patch_lock:
        instances = list(reversed(_active_instances))

    for inst in instances:
        if host in inst._host_list or inst._patterns_list:
            return inst

    for inst in instances:
        if host in inst._passthrough_hosts or inst.passthrough_unmatched:
            return None

    # No instance claims this host and none allow passthrough — redirect to the
    # innermost instance's server so the client gets a clear connection error.
    return instances[0] if instances else None


async def _shared_resolve(
    resolver_self: "AbstractResolver",
    host: str,
    port: int = 0,
    family: "socket.AddressFamily" = socket.AF_INET,
) -> "list[ResolveResult]":
    inst = _resolution_target(host)
    if inst is not None:
        return [_make_resolve_result(host, inst, family)]
    return await _pick_real_resolver(resolver_self)(resolver_self, host, port, family)


async def _shared_get(connector_self: "TCPConnector", key: Any, traces: Any) -> Any:
    # A pooled keep-alive connection to the real server bypasses DNS resolution
    # entirely, so it would leak past the mock. For any host a mock would
    # intercept, refuse to reuse a pooled connection — forcing aiohttp to open a
    # fresh one, which then routes through the patched resolver to the mock.
    if _resolution_target(key.host) is not None:
        return None
    return await _real_get(connector_self, key, traces)


def _shared_ssl_context(connector_self: "TCPConnector", req: "ClientRequest") -> "SSLContext | None":
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
        if inst._patterns_list and (inst.passthrough_unmatched or host in inst._passthrough_hosts):
            if req.url.scheme == "https":
                req.headers["X-Aiointercept-Orig-Scheme"] = "https"
            return None

    return _real_ssl_context(connector_self, req)  # type: ignore[misc]


def _install_patches(inst: "aiointercept") -> None:
    """Register ``inst`` and, on the first active instance, patch the resolver,
    SSL context, host resolution, and connection reuse at the class level."""
    global _patch_refcount, _real_threaded_resolve, _real_async_resolve, _real_ssl_context
    global _real_resolve_host, _real_get
    with _patch_lock:
        _active_instances.append(inst)
        if _patch_refcount == 0:
            _real_threaded_resolve = ThreadedResolver.resolve
            _real_async_resolve = AsyncResolver.resolve
            _real_ssl_context = TCPConnector._get_ssl_context  # pyright: ignore[reportPrivateUsage]
            _real_resolve_host = TCPConnector._resolve_host  # pyright: ignore[reportPrivateUsage]
            _real_get = TCPConnector._get  # pyright: ignore[reportPrivateUsage]
            ThreadedResolver.resolve = _shared_resolve  # type: ignore[assignment]
            AsyncResolver.resolve = _shared_resolve  # type: ignore[assignment]
            TCPConnector._get_ssl_context = _shared_ssl_context  # type: ignore[assignment]
            TCPConnector._resolve_host = _shared_resolve_host  # type: ignore[assignment]
            TCPConnector._get = _shared_get  # type: ignore[assignment]
        _patch_refcount += 1


def _remove_patches(inst: "aiointercept") -> None:
    """Deregister ``inst`` and, once the last active instance is gone, restore
    the original resolver, SSL context, and host resolution. Idempotent: a no-op
    if ``inst`` was never registered (e.g. ``start()`` failed before patching)."""
    global _patch_refcount, _real_threaded_resolve, _real_async_resolve, _real_ssl_context
    global _real_resolve_host, _real_get
    with _patch_lock:
        if inst not in _active_instances:
            return
        _active_instances.remove(inst)
        _patch_refcount -= 1
        if _patch_refcount == 0:
            ThreadedResolver.resolve = _real_threaded_resolve  # type: ignore[method-assign]
            AsyncResolver.resolve = _real_async_resolve  # type: ignore[method-assign]
            TCPConnector._get_ssl_context = _real_ssl_context  # type: ignore[method-assign,reportPrivateUsage]
            TCPConnector._resolve_host = _real_resolve_host  # type: ignore[method-assign,reportPrivateUsage]
            TCPConnector._get = _real_get  # type: ignore[method-assign,reportPrivateUsage]
            _real_threaded_resolve = None
            _real_async_resolve = None
            _real_ssl_context = None
            _real_resolve_host = None
            _real_get = None


class CallbackResult:
    """Result object returned by a callback.

    Args:
        method: HTTP method (default GET; not used by the server handler).
        status: HTTP response status code.
        body: Raw response body as str or bytes.
        content_type: ``Content-Type`` header value. Set to ``None`` when
            *headers* already carries a ``Content-Type`` entry, to avoid
            colliding with it.
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
        response_class: type[ClientResponse] | None = None,
        reason: str | None = None,
    ):
        self.method = method
        self.status = status
        self.body = body
        self.payload = payload
        # Drop the default content_type when the caller already supplied a
        # Content-Type header (case-insensitively), to avoid the ValueError
        # aiohttp.web.Response raises when both are set.
        has_content_type_header = headers is not None and any(k.lower() == "content-type" for k in headers)
        self.content_type: str | None = None if has_content_type_header else content_type
        self.headers = headers
        self.response_class = response_class
        self.reason = reason


class _CloseConnection:
    """Sentinel: handler should close the transport, surfacing ClientConnectionError on the client."""


_CLOSE_CONNECTION = _CloseConnection()
handler_type = Callable[[web.Request], Awaitable[web.StreamResponse]] | _CloseConnection


class aiointercept:  # noqa: N801
    """Mock :mod:`aiohttp` requests by routing them through a real test server.

    Starts a real :class:`aiohttp.web.Application` on localhost and directs the
    client's requests to it. In the default mode you point the client at
    :attr:`server_url`; with ``mock_external_urls=True`` the DNS resolver is
    patched at the class level so any aiohttp request to a registered host is
    transparently intercepted. Register responses with :meth:`add` or the
    per-method shortcuts (:meth:`get`, :meth:`post`, ...), then inspect what was
    sent via :attr:`requests` and the ``assert_*`` helpers.

    Use it as an async context manager, as a decorator, or drive the lifecycle
    explicitly with :meth:`start` / :meth:`stop`::

        async with aiointercept() as m:
            m.get(f"{m.server_url}/users", payload=[{"id": 1}])
            async with aiohttp.ClientSession() as session:
                resp = await session.get(f"{m.server_url}/users")

    Attributes:
        server_url: Base URL of the running test server, e.g.
            ``"http://127.0.0.1:54321"``. Set once :meth:`start` has run.
        server_host: Host the test server is bound to.
        server_port: Port the test server is listening on.
        requests: Mapping of ``(METHOD, normalized URL)`` to the list of
            :class:`AiointerceptRequest` objects received, in arrival order.
    """

    def __init__(
        self,
        mock_external_urls: bool = False,
        passthrough: Sequence[str] | None = None,
        passthrough_unmatched: bool = False,
        param: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a mock.

        Args:
            mock_external_urls: When ``True``, patch the DNS resolver at the
                process level so requests to any registered host are intercepted,
                even those made by third-party libraries. When ``False``
                (default), only requests sent to :attr:`server_url` are
                intercepted and no global state is modified.
            passthrough: Hosts whose requests bypass the mock and reach the real
                network. Requires ``mock_external_urls=True``.
            passthrough_unmatched: When ``True``, proxy every unmatched request
                to the real network instead of failing it. Requires
                ``mock_external_urls=True``.
            param: Keyword-argument name under which the mock is injected when
                the instance is used as a decorator. Defaults to appending it as
                the last positional argument.
        """
        if kwargs:
            warnings.warn(
                "Passing extra parameters to aiointercept via kwargs is deprecated "
                "and will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )

        if passthrough_unmatched and not mock_external_urls:
            raise ValueError("passthrough_unmatched=True requires mock_external_urls=True")

        self._passthrough_urls = passthrough or []
        self._passthrough_hosts: list[str] = []
        self._mock_external_urls = mock_external_urls

        if mock_external_urls:
            for p in self._passthrough_urls:
                host = URL(p).host
                self._passthrough_hosts.append(host if host else p)

        self.param = param
        self.passthrough_unmatched = passthrough_unmatched

        self._host_list: set[str] = set()
        self._https_hosts: set[str] = set()
        self._patterns_list: list[Pattern[str]] = []

        # handler are (path, method) → handler or list of handlers (if repeat != True)
        self.handlers: dict[tuple[str, str], handler_type | list[handler_type]] = {}
        # patterns_handler are (pattern, method) → handler or list of handlers (if repeat != True)
        self.patterns_handler: dict[tuple[Pattern[str], str], handler_type | list[handler_type]] = {}

        # recorded requests: {(METHOD, URL): [web.Request, ...]}
        self.requests: dict[tuple[str, URL], list[AiointerceptRequest]] = {}

        self.server: TestServer | None = None
        self._bypass_session: aiohttp.ClientSession | None = None
        # The intercept server runs on its own daemon thread + event loop so
        # that callers whose loop gets blocked (e.g. Starlette TestClient
        # holding the loop during a sync request) cannot deadlock the server.
        self._server_thread: threading.Thread | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        # The loop the caller entered the context manager on. Async user
        # callbacks are scheduled back onto this loop so they observe the
        # same loop-bound primitives (asyncio.Event, asyncio.Queue, ...) as
        # the rest of the test.
        self._caller_loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> "aiointercept":
        await self.start()
        return self

    async def start(self) -> None:
        """Start the test server and install patches (if any).

        Called automatically by ``__aenter__``. Call it directly from a
        framework's setup hook (e.g. ``asyncSetUp``) when not using the context
        manager. After this returns, :attr:`server_url` is available. Pair every
        ``start()`` with a :meth:`stop`.
        """
        self._caller_loop = asyncio.get_running_loop()
        await self._start_server_thread()
        assert self._server_loop is not None
        assert self.server is not None

        assert isinstance(self.server.host, str)
        assert isinstance(self.server.port, int)
        self.server_host = self.server.host
        self.server_port = self.server.port
        self.server_url = f"http://{self.server_host}:{self.server.port}"

        try:
            if self._mock_external_urls:
                _install_patches(self)

                # ClientSession binds to the running loop at construction time,
                # so build it on the server loop (where _dispatch will use it).
                async def _create_bypass() -> None:
                    self._bypass_session = self._make_bypass_session()

                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_create_bypass(), self._server_loop))
        except BaseException:
            if self._mock_external_urls:
                _remove_patches(self)
            await self._stop_server_thread()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.stop()

    async def stop(self) -> None:
        """Stop the test server and remove any DNS/SSL patches.

        Called automatically by ``__aexit__``. When DNS patching is active, the
        patches are only fully removed once the last concurrently-running
        instance stops.
        """
        try:
            if self._mock_external_urls:
                _remove_patches(self)
        finally:
            await self._stop_server_thread()
            self._caller_loop = None
            self.handlers.clear()
            self.patterns_handler.clear()
            self._host_list.clear()
            self._https_hosts.clear()
            self._patterns_list.clear()

    async def _start_server_thread(self) -> None:
        ready = threading.Event()
        startup_error: list[BaseException] = []

        def _run_server_thread() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._server_loop = loop

            async def _start_server() -> None:
                app = web.Application()
                app.router.add_route("*", "/{tail:.*}", self._dispatch)
                self.server = TestServer(app)
                await self.server.start_server()

            try:
                loop.run_until_complete(_start_server())
            except BaseException as e:
                startup_error.append(e)
                ready.set()
                loop.close()
                self._server_loop = None
                return

            ready.set()
            try:
                loop.run_forever()
            finally:
                # Drain anything still scheduled before closing the loop.
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                finally:
                    loop.close()

        self._server_thread = threading.Thread(target=_run_server_thread, name="aiointercept-server", daemon=True)
        self._server_thread.start()
        # ready.wait runs on the caller's loop thread; the server thread sets
        # the event quickly so a brief blocking wait here is acceptable.
        ready.wait()
        if startup_error:
            self._server_thread.join()
            self._server_thread = None
            raise startup_error[0]

    async def _stop_server_thread(self) -> None:
        server_loop = self._server_loop
        if server_loop is not None and server_loop.is_running():

            async def _teardown() -> None:
                if self._bypass_session is not None:
                    await self._bypass_session.close()
                    self._bypass_session = None
                if self.server is not None:
                    await self.server.close()
                    self.server = None

            try:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_teardown(), server_loop))
            finally:
                server_loop.call_soon_threadsafe(server_loop.stop)

        if self._server_thread is not None:
            self._server_thread.join()
            self._server_thread = None
        self._server_loop = None
        self._bypass_session = None
        self.server = None

    # Decorator support
    def __call__(self, f: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(f)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with self as m:
                if self.param:
                    kwargs[self.param] = m
                elif args and hasattr(args[0], f.__name__):
                    args = (args[0], m, *args[1:])
                else:
                    args = (*args, m)
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

        return aiohttp.ClientSession(connector=_BypassConnector(resolver=_BypassResolver()))

    def _match_pattern(self, url: str) -> bool:
        return any(p.match(url) for p in self._patterns_list)

    def _diagnose_unmatched(self, method: str, url_str: str) -> str:
        request_line = f"{method} {url_str}"
        existing = self.handlers.get((url_str, method))
        if isinstance(existing, list) and not existing:
            return (
                f"aiointercept: no handler for {request_line} — "
                "all registered handlers for this method+url have already been consumed "
                "(finite repeat exhausted)"
            )
        candidates = sorted({f"{m} {u}" for (u, m) in self.handlers})
        close = difflib.get_close_matches(request_line, candidates, n=1, cutoff=0.0) if candidates else []
        if close:
            diff = "\n".join(line.rstrip() for line in difflib.ndiff([request_line], [close[0]]))
            return f"aiointercept: no matching handler — closest registered (diff):\n{diff}"
        if self.patterns_handler:
            patterns = sorted({p.pattern for (p, _) in self.patterns_handler})
            return f"aiointercept: no handler for {request_line} — no exact handlers; registered patterns: {patterns}"
        return f"aiointercept: no handler for {request_line} — no handlers registered."

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        url = normalize_url(request.url)
        req_host = request.headers.get("Host", "")
        if request.headers.get("X-Aiointercept-Orig-Scheme") == "https":
            self._https_hosts.add(req_host)
        if req_host in self._https_hosts:
            url = url.with_scheme("https")

        key = (request.method.upper(), url)
        self.requests.setdefault(key, [])
        captured_body = None
        json = None
        if request.can_read_body:
            captured_body = await request.read()
            with suppress(Exception):
                json = json_module.loads(captured_body) if captured_body else None
        # this kwargs will be removed, should be deprecated in the future
        request_kwargs: AiointerceptRequestKwargs = {
            "headers": request.headers,
            # Use getall so duplicate keys (?a=1&a=2) aren't collapsed to one value.
            "query": {k: request.query.getall(k) for k in dict.fromkeys(request.query)},
            "json": json,
            "data": captured_body,
        }

        aiointercept_request = AiointerceptRequest.upgrade(request, captured_body, request_kwargs, url)
        # Read body eagerly before the handler runs, because aiohttp sets
        # PayloadAccessError on the stream once the response cycle completes.
        self.requests[key].append(aiointercept_request)
        url_str = str(url)
        selected_handler = self.handlers.get((url_str, request.method))
        if isinstance(selected_handler, list):
            if not selected_handler:
                handler: handler_type | None = None
            else:
                handler = typing.cast("handler_type", selected_handler.pop(0))

        else:
            handler = selected_handler
        original_host = request.headers.get("Host", request.url.host)
        if handler is None:
            original_urls = [
                f"https://{original_host}{request.path_qs}",
                f"http://{original_host}{request.path_qs}",
            ]
            for (pattern, method), pattern_handler in self.patterns_handler.items():
                if any(pattern.match(u) for u in original_urls) and method == request.method:
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
            if self._mock_external_urls and self.passthrough_unmatched:
                scheme = request.headers.get("X-Aiointercept-Orig-Scheme") or ("https" if request.secure else "http")
                real_url = f"{scheme}://{original_host}{request.path_qs}"
                session = self._bypass_session
                assert session is not None, "Bypass session not initialized"
                async with session.request(
                    method=request.method,
                    url=real_url,
                    headers={k: v for k, v in request.headers.items() if k.lower() not in _PROXY_REQ_DROP},
                    data=getattr(request, "captured_body", None) or None,
                    allow_redirects=True,
                    ssl=True,
                ) as real_resp:
                    body = await real_resp.read()
                    return web.Response(
                        status=real_resp.status,
                        headers={k: v for k, v in real_resp.headers.items() if k.lower() not in _PROXY_RESP_DROP},
                        body=body,
                    )
            warning_msg = self._diagnose_unmatched(request.method.upper(), str(url))
            logger.warning(warning_msg)
            # this should raise ClientConnectionError on the other side
            if request.transport:
                request.transport.close()
            # Fallback in case transport.close() didn't take effect — the client
            # should normally see ClientConnectionError before reading this body.
            return web.Response(status=502, text="No handler registered for this request.")
        if handler is _CLOSE_CONNECTION:
            if request.transport:
                request.transport.close()
            # Fallback in case transport.close() didn't take effect — the client
            # should normally see ClientConnectionError before reading this body.
            return web.Response(status=502, text="Handler registered to raise ClientConnectionError.")
        callable_handler = cast("Callable[[web.Request], Awaitable[web.StreamResponse]]", handler)
        return await callable_handler(request)

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
        callback: Callable[..., CallbackResult | Awaitable[CallbackResult]] | None = None,
        reason: str | None = None,
        exception: Exception | bool | None = None,
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
        if exception and exception is not True:
            logger.warning(
                "aiointercept only raise ClientConnectionError, pass exception=True instead of an specific exception"
            )
        method = method.upper()
        if isinstance(url, str):
            url = URL(url)

        if isinstance(url, Pattern):
            self._patterns_list.append(url)

        assert self.server is not None, "Server not started — use `async with aiointercept() as m:` first."
        if isinstance(url, URL):
            host = url.host
            assert host, f"Cannot extract host from {url!r}"

            # Map this host → our test server
            self._host_list.add(host)
            # Record HTTPS-ness eagerly so it survives clear() + re-add. The
            # SSL-context hook also populates _https_hosts, but only on fresh
            # TCP connections; keep-alive connections that outlive a clear()
            # would otherwise lose their scheme tagging.
            if url.scheme == "https":
                self._https_hosts.add(host)

        if json is not None:
            body = json_module.dumps(json).encode()
        elif payload is not None:
            body = json_module.dumps(payload).encode()
        elif isinstance(body, str):
            body = body.encode()

        resp_headers = dict(headers or {})
        if not content_type and not any(k.lower() == "content-type" for k in resp_headers):
            content_type = "application/json"

        async def handler(request: web.Request) -> web.Response:
            if callable(callback):
                aiointercept_request = cast("AiointerceptRequest", request)
                cb_kwargs = aiointercept_request.kwargs
                # Use the canonical URL (normalized, original scheme restored)
                # rather than request.url, which is always http:// here because
                # the test server receives the request over plain HTTP.
                cb_url = aiointercept_request.canonical_url
                if inspect.iscoroutinefunction(callback):
                    # Async callbacks run on the caller's loop so that
                    # loop-bound primitives (asyncio.Event, asyncio.Queue,
                    # asyncio.Lock created by the test) keep working.
                    caller_loop = self._caller_loop
                    if (
                        caller_loop is not None
                        and not caller_loop.is_closed()
                        and caller_loop is not asyncio.get_running_loop()
                    ):
                        result = await asyncio.wrap_future(
                            asyncio.run_coroutine_threadsafe(callback(cb_url, **cb_kwargs), caller_loop)
                        )
                    else:
                        result = await callback(cb_url, **cb_kwargs)
                else:
                    result = callback(cb_url, **cb_kwargs)
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

        handler_or_exc: handler_type = handler if not exception else _CLOSE_CONNECTION
        if repeat is True:
            if isinstance(url, Pattern):
                self.patterns_handler[url, method] = handler_or_exc
                return
            handler_url = str(normalize_url(url))
            self.handlers[handler_url, method] = handler_or_exc
        else:
            if repeat is False or repeat == 0:
                repeat = 1
            if repeat < 1:
                raise ValueError("repeat must be at least 1")
            handlers: list[handler_type] = [handler_or_exc] * repeat
            if isinstance(url, Pattern):
                if (url, method) in self.patterns_handler:
                    list_pattern_handler = self.patterns_handler[(url, method)]
                    if isinstance(list_pattern_handler, list):
                        list_pattern_handler = typing.cast("list[handler_type]", list_pattern_handler)
                        list_pattern_handler += handlers
                    else:
                        raise ValueError(
                            f"Existing handler for pattern {url} {method} has "
                            "repeat=True, cannot add more handlers to it."
                        )

                else:
                    self.patterns_handler[url, method] = handlers
                return
            handler_url = str(normalize_url(url))
            if (handler_url, method) in self.handlers:
                handlers_list = self.handlers[(handler_url, method)]
                if isinstance(handlers_list, list):
                    handlers_list = typing.cast("list[handler_type]", handlers_list)
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
        self._https_hosts.clear()

    def assert_called(self) -> None:
        """Assert that at least one request was made."""
        if not self.requests:
            raise AssertionError("Expected at least one call, got none.")

    def assert_not_called(self) -> None:
        """Assert that no requests were made."""
        if self.requests:
            raise AssertionError(f"Expected no calls, got {sum(len(v) for v in self.requests.values())}.")

    def assert_called_once(self) -> None:
        """Assert that exactly one request was made across all URLs."""
        count = sum(len(v) for v in self.requests.values())
        if count != 1:
            raise AssertionError(f"Expected exactly 1 call, got {count}.")

    def _no_calls_message(self, method: str, url: URL) -> str:
        """Build a 'No calls to ...' message, suggesting similar recorded calls."""
        msg = f"No calls to {method.upper()} {url}"
        recorded = [f"{m} {u}" for (m, u) in self.requests]
        if not recorded:
            return f"{msg}\nNo requests were recorded."
        target = f"{method.upper()} {url}"
        similar = difflib.get_close_matches(target, recorded, n=3, cutoff=0)
        heading = "Did you mean one of"
        listing = "\n".join(f"  - {s}" for s in similar)
        return f"{msg}\n{heading}:\n{listing}"

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
            raise AssertionError(self._no_calls_message(method, url))

    def assert_called_with(
        self,
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: typing.Mapping[str, str] | None = None,
        data: str | bytes | typing.Mapping[str, Any] | None = None,
        json: Any = None,
        headers: typing.Mapping[str, str] | None = None,
        strict_headers: bool = False,
        **kwargs: Any,
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
            kwargs: Ignored (present for aioresponses API compatibility).
        """
        if kwargs:
            warnings.warn(
                "Passing extra parameters to assert_called_with via kwargs is "
                "deprecated and will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(self._no_calls_message(method, url))
        request: AiointerceptRequest = self.requests[key][-1]
        actual_body = request.captured_body
        if json is not None and json is not mock.ANY:
            # aiohttp sends json= as JSON-encoded bytes with application/json
            actual_body_str = actual_body.decode(errors="replace")
            try:
                actual_json = json_module.loads(actual_body_str)  # type: ignore[attr-defined]
            except Exception as exc:
                raise AssertionError(_diff("Expected JSON body, got non-JSON body", json, actual_body)) from exc
            assert actual_json == json, _diff("JSON body mismatch", json, actual_json)
        elif data is not None and data is not mock.ANY:
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
                assert actual_qs == expected_qs, _diff("Form-encoded body mismatch", expected_qs, actual_qs)
            else:
                expected_body = data.encode() if isinstance(data, str) else data
                assert actual_body == expected_body, _diff("Body mismatch", expected_body, actual_body)
        if strict_headers:
            actual_headers = dict(request.headers)
            actual_headers.pop("x-aiointercept-orig-scheme", None)
            expected_headers = headers or {}
            assert expected_headers == actual_headers, _diff(
                "Headers mismatch", dict(expected_headers), dict(actual_headers)
            )
        elif headers and headers is not mock.ANY:
            actual_headers_proxy = request.headers
            for k, v in headers.items():
                assert actual_headers_proxy.get(k) == v, (
                    f"Header {k!r}: expected {v!r}, got {actual_headers_proxy.get(k)!r}"
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
        **kwargs: Any,
    ) -> None:
        """Assert that exactly one request was made and it matched the given arguments."""
        self.assert_called_once()
        self.assert_called_with(url, method, params, data, json, headers, strict_headers, **kwargs)
