Usage
=====

Interception modes
------------------

``mock_external_urls=False`` (default)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The server starts on localhost but DNS is **not** patched. Point your client
directly at ``m.server_url``. This is the safest mode — no global state is
modified.

.. code-block:: python

    async with aiointercept() as m:
        m.get(f"{m.server_url}/api/users", payload=[{"id": 1}])

        async with aiohttp.ClientSession() as session:
            resp = await session.get(f"{m.server_url}/api/users")
            assert resp.status == 200

Use ``m.server_url`` as a ``base_url`` to keep your code clean:

.. code-block:: python

    async with aiointercept() as m:
        m.get(f"{m.server_url}/api/users", payload=[{"id": 1}])

        async with aiohttp.ClientSession(base_url=m.server_url) as session:
            resp = await session.get("/api/users")

``mock_external_urls=True``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Patches the DNS resolver at the **process level** so every aiohttp request is
redirected to the mock server — even those made by third-party libraries you
cannot modify.

.. code-block:: python

    async with aiointercept(mock_external_urls=True) as m:
        m.get("https://api.stripe.com/v1/charges", payload={"data": []})

        # Code under test calls the real Stripe URL internally
        result = await billing_service.list_charges()
        assert result == []

.. warning::

    DNS patching affects the whole process for the duration of the block. It
    does not intercept requests to bare IP addresses.


Registering mock responses
--------------------------

``add(url, method, ...)``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    m.add(
        url,                    # str | yarl.URL | re.Pattern
        method="GET",           # HTTP method (case-insensitive)
        status=200,
        body=b"",               # raw response body (str is UTF-8 encoded;
                                # an AsyncIterable[bytes] streams chunked)
        json=None,              # serialized to JSON, overrides body
        payload=None,           # alias for json
        headers=None,           # extra response headers
        content_type=None,      # overrides Content-Type
        repeat=False,           # True = infinite; int N = exactly N times
        callback=None,          # callable or coroutine -> CallbackResult
        reason=None,            # HTTP reason phrase
        exception=None,         # truthy -> close connection (ClientConnectionError)
    )

HTTP method shortcuts
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    m.get(url, **kwargs)
    m.post(url, **kwargs)
    m.put(url, **kwargs)
    m.patch(url, **kwargs)
    m.delete(url, **kwargs)
    m.head(url, **kwargs)
    m.options(url, **kwargs)

All shortcuts forward their keyword arguments to
:meth:`~aiointercept.aiointercept.add`.

Regex patterns
~~~~~~~~~~~~~~~

Pass a compiled :class:`re.Pattern` to match a family of URLs:

.. code-block:: python

    import re

    pattern = re.compile(r"^https://api\.example\.com/users/\d+$")
    m.get(pattern, payload={"id": 1, "name": "Alice"})

    # Matches https://api.example.com/users/1, /users/42, etc.

Repeat and response queuing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    # Respond to every request (indefinite):
    m.get(url, repeat=True, payload={"ok": True})

    # Respond exactly 3 times, then raise ClientConnectionError:
    m.get(url, repeat=3, status=200)

    # Queue different responses by calling add() multiple times:
    m.post(url, status=201, payload={"created": True})
    m.post(url, status=409, payload={"error": "conflict"})
    # First POST -> 201, second POST -> 409, third POST -> ClientConnectionError


Streaming response bodies
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pass an ``AsyncIterable[bytes]`` (e.g. an async generator) as ``body`` to stream
the response chunk by chunk. aiohttp wraps it in an ``AsyncIterablePayload`` and
sends the chunks with chunked transfer encoding, so the client receives each
piece as it is written — handy for mocking server-sent events or
OpenAI-style streaming endpoints.

.. code-block:: python

    async def event_stream():
        yield b'data: {"delta": "Hello"}\n\n'
        yield b'data: {"delta": " world"}\n\n'
        yield b"data: [DONE]\n\n"

    async with aiointercept(mock_external_urls=True) as m:
        # body defaults to Content-Type: application/json — set content_type
        # explicitly so the mock matches what a real SSE endpoint returns.
        m.get(
            "https://api.openai.com/v1/stream",
            body=event_stream(),
            content_type="text/event-stream",
        )

        async with aiohttp.ClientSession() as session:
            resp = await session.get("https://api.openai.com/v1/stream")
            async for chunk in resp.content.iter_any():
                handle(chunk)   # each yielded piece arrives as it is written

.. note::

   The async iterator is drained once. Combined with ``repeat`` only the first
   response carries the body and later ones come back empty, so register a fresh
   iterator per expected request instead.


Callbacks
---------

Use a callback when the response depends on the request:

.. code-block:: python

    from aiointercept import aiointercept, CallbackResult

    def echo_callback(url, *, headers, query, json, **kwargs):
        return CallbackResult(status=200, payload={"echo": json})

    async def test_echo():
        async with aiointercept() as m:
            m.post(f"{m.server_url}/echo", callback=echo_callback)
            ...

Async callbacks are also supported:

.. code-block:: python

    async def async_callback(url, **kwargs):
        await asyncio.sleep(10)
        return CallbackResult(body=b"async response")

    async def test_slow():
        async with aiointercept() as m:
            m.get(f"{m.server_url}/slow", callback=async_callback)
            ...

A callback returns a :class:`~aiointercept.CallbackResult`:

.. list-table::
   :header-rows: 1
   :widths: 18 22 14 46

   * - Field
     - Type
     - Default
     - Description
   * - ``status``
     - ``int``
     - ``200``
     - Response status code
   * - ``body``
     - ``str | bytes``
     - ``""``
     - Raw response body
   * - ``payload``
     - ``Any``
     - ``None``
     - Response body serialized to JSON (overrides ``body``)
   * - ``headers``
     - ``dict[str, str] | None``
     - ``None``
     - Extra response headers
   * - ``content_type``
     - ``str``
     - ``"application/json"``
     - ``Content-Type`` header
   * - ``reason``
     - ``str | None``
     - ``None``
     - HTTP reason phrase


Passthrough
-----------

Let specific hosts or all unmatched requests reach the real network. Only
available with ``mock_external_urls=True``.

.. code-block:: python

    # Specific hosts bypass the mock:
    async with aiointercept(
        mock_external_urls=True,
        passthrough=["https://real-api.example.com"],
    ) as m:
        m.get("https://mocked.example.com/data", payload={"mocked": True})
        # Requests to real-api.example.com go to the real server.

    # All unmatched requests go to the real server:
    async with aiointercept(
        mock_external_urls=True,
        passthrough_unmatched=True,
    ) as m:
        m.get("https://mocked.example.com/data", payload={"mocked": True})
        # Any other URL is proxied to the real network.

.. warning::

   Passthrough requests reach the real network with their original headers —
   including any ``Authorization`` or ``Cookie`` values. With
   ``passthrough_unmatched=True``, a typo'd URL in a test can silently send
   real credentials to a real server instead of failing.


Inspecting requests
-------------------

All intercepted requests are stored in ``m.requests``, keyed by
``(METHOD, normalized_url)``:

.. code-block:: python

    from yarl import URL

    key = ("POST", URL("https://api.example.com/orders"))
    req = m.requests[key][-1]   # most recent request to this URL

    req.captured_body            # raw bytes body
    req.kwargs["json"]           # parsed JSON body (or None)
    req.kwargs["query"]          # dict[str, list[str]] - preserves duplicate keys
    req.kwargs["headers"]        # raw request headers (multidict)

URLs are normalized: fragments are stripped and query parameters are sorted.

For cross-URL ordering, ``m.ordered_requests`` is a flat list of
``(key, request)`` tuples in the order they arrived:

.. code-block:: python

    # Iterate all requests in arrival order:
    for (method, url), req in m.ordered_requests:
        print(method, url, req.kwargs["json"])

    # Quick helpers built on top of ordered_requests:
    m.call_count    # int — total requests across all URLs
    m.last_request  # AiointerceptRequest | None — the most recent request

Per-handler call counts
-----------------------

Every registration call (``m.add``, ``m.get``, ``m.post``, etc.) returns a
:class:`~aiointercept.MockResponse` whose ``call_count`` attribute increments
each time that specific mock is matched:

.. code-block:: python

    rsp = m.get("https://api.example.com/items", payload=[])
    rsp2 = m.post("https://api.example.com/items", status=201)

    await session.get("https://api.example.com/items")
    await session.get("https://api.example.com/items")
    await session.post("https://api.example.com/items", json={"name": "foo"})

    assert rsp.call_count == 2
    assert rsp2.call_count == 1

With ``repeat=N``, all N slots share the same :class:`~aiointercept.MockResponse`
object, so ``call_count`` correctly accumulates across every matching request
regardless of how many slots remain.


Constructor parameters
----------------------

.. list-table::
   :header-rows: 1
   :widths: 22 18 12 48

   * - Parameter
     - Type
     - Default
     - Description
   * - ``mock_external_urls``
     - ``bool``
     - ``False``
     - When ``True``, patches the DNS resolver so external URLs are
       intercepted. When ``False``, only requests to ``m.server_url`` are
       intercepted.
   * - ``passthrough``
     - ``list[str] | None``
     - ``None``
     - Hosts whose requests bypass the mock and reach the real network.
       Requires ``mock_external_urls=True``.
   * - ``passthrough_unmatched``
     - ``bool``
     - ``False``
     - Proxy all unmatched requests to the real network. Requires
       ``mock_external_urls=True``.
   * - ``param``
     - ``str | None``
     - ``None``
     - Kwarg name under which the mock is injected when used as a decorator.
