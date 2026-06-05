Assertions
==========

``aiointercept`` records every intercepted request in ``m.requests`` and
provides a set of assertion helpers inspired by :mod:`unittest.mock`.

.. code-block:: python

    m.assert_called()              # at least one request was made
    m.assert_not_called()          # no requests were made
    m.assert_called_once()         # exactly one request across all URLs

    m.assert_any_call(url, method="GET", params=None)
    m.assert_called_with(url, method="GET", params=None, data=None,
                         json=None, headers=None, strict_headers=False)
    m.assert_called_once_with(url, ...)

:meth:`~aiointercept.aiointercept.assert_called_with` inspects the **most
recent** call to the given URL.

.. code-block:: python

    # Check the JSON body of the last POST to /orders:
    m.assert_called_with(
        "https://api.example.com/orders",
        method="POST",
        json={"item": "book", "qty": 2},
    )

    # Check specific headers (subset by default):
    m.assert_called_with(url, headers={"Authorization": "Bearer token123"})

    # Require the full header map to match exactly:
    m.assert_called_with(url, headers={"X-Custom": "value"}, strict_headers=True)

    # Check form-encoded body:
    m.assert_called_with(url, method="POST",
                         data={"username": "alice", "password": "s3cr3t"})
