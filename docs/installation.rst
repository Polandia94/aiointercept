Installation
============

Install from PyPI:

.. code-block:: bash

    pip install aiointercept

**Requirements:** Python ≥ 3.10, aiohttp ≥ 3.13.

For the bundled pytest fixtures, install the ``pytest-asyncio`` extra:

.. code-block:: bash

    pip install 'aiointercept[pytest-asyncio]'

The extra requires pytest-asyncio 0.24 or newer. The plugin registers its
fixtures only when a compatible pytest-asyncio is installed; otherwise it is
inactive.

Coming from aioresponses?
-------------------------

``aiointercept`` aims to be a near drop-in replacement for ``aioresponses``.
The :doc:`migration guide <migrating>` covers every breaking change: context
manager usage, fixture patterns, URL registration, ``exception=``, callbacks,
and assertion helpers.

If you find an incompatibility not covered by the migration guide, please
`open an issue <https://github.com/Polandia94/aiointercept/issues>`_.
