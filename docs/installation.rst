Installation
============

Install from PyPI:

.. code-block:: bash

    pip install aiointercept

**Requirements:** Python ≥ 3.10, aiohttp ≥ 3.13.

Coming from aioresponses?
-------------------------

``aiointercept`` aims to be a near drop-in replacement for ``aioresponses``.
The :doc:`migration guide <migrating>` covers every breaking change: context
manager usage, fixture patterns, URL registration, ``exception=``, callbacks,
and assertion helpers.

If you find an incompatibility not covered by the migration guide, please
`open an issue <https://github.com/Polandia94/aiointercept/issues>`_.
