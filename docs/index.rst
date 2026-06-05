aiointercept
============

Mock :mod:`aiohttp` HTTP requests by routing them through a real
:class:`aiohttp.web.Application` test server. Inspired by ``aioresponses``,
with a largely compatible API.

.. code-block:: python

    import aiohttp
    from aiointercept import aiointercept

    async with aiointercept() as m:
        m.get(f"{m.server_url}/users", payload=[{"id": 1}])

        async with aiohttp.ClientSession() as session:
            resp = await session.get(f"{m.server_url}/users")
            assert await resp.json() == [{"id": 1}]


Why aiointercept?
-----------------

Testing code that makes HTTP requests usually means either hitting a real
server (slow, fragile, requires network) or replacing the HTTP layer with fake
objects (fast, but disconnected from reality).

``aiointercept`` takes a third path: it starts a real :mod:`aiohttp.web` server
on localhost and redirects your client's requests to it — either by pointing
the client at ``m.server_url`` directly, or by patching the DNS resolver so
existing URLs are transparently intercepted. Your code runs its full HTTP
stack; only the remote endpoint is replaced.

This gives you:

* **Real serialization.** Headers, body encoding, and content-type negotiation
  all go through the actual aiohttp stack, so bugs that only appear during
  serialization are caught.
* **Inspectable requests.** Callbacks receive a real
  :class:`aiohttp.web.Request` — you can read the body, headers, query params,
  and anything else the server would see.
* **Minimal patching.** The default mode touches nothing globally — your client
  just talks to a local server. When you need to intercept hardcoded URLs, only
  the DNS resolver is patched (not aiohttp's request pipeline), so concurrent
  requests, redirects, and connection pooling still behave as in production.


.. admonition:: Coming from aioresponses?

   ``aiointercept`` is a near drop-in replacement. Start with the
   :doc:`migration guide <migrating>`.


Contents
--------

.. toctree::
   :maxdepth: 2

   installation
   migrating
   quickstart
   usage
   assertions
   api
   changelog


Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
