Migrating from aioresponses
===========================

``aiointercept`` is designed as a near drop-in replacement for
``aioresponses``: replace it with ``aiointercept(mock_external_urls=True)`` and
your URLs and response registration stay the same. The full guide below walks 
through every breaking change.

.. include:: ../MIGRATING.md
   :parser: myst_parser.sphinx_
   :start-line: 1
