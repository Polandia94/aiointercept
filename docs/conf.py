"""Sphinx configuration for the aiointercept documentation."""

from importlib import metadata

# -- Project information -----------------------------------------------------

project = "aiointercept"
copyright = "2026, Pablo Estevez"
author = "Pablo Estevez"

try:
    release = metadata.version("aiointercept")
except metadata.PackageNotFoundError:  # pragma: no cover - docs built from source tree
    release = "0.0.0"
# The short X.Y version.
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.todo",
    "myst_parser",
]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Treat warnings from undocumented references gently; library has light docstrings.
nitpicky = False

# The changelog is included from CHANGELOG.md starting below its H1 title, so its
# sections legitimately begin at H2 — silence the resulting header warnings.
suppress_warnings = ["myst.header"]

# -- Autodoc -----------------------------------------------------------------

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autoclass_content = "both"
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}

napoleon_google_docstring = True
napoleon_numpy_docstring = True
# Render "Attributes:" sections as inline :ivar: fields so they don't collide
# with the same names documented by autodoc's :members:.
napoleon_use_ivar = True

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "aiohttp": ("https://docs.aiohttp.org/en/stable", None),
    "yarl": ("https://yarl.aio-libs.org/en/stable", None),
    "multidict": ("https://multidict.aio-libs.org/en/stable", None),
}

# -- HTML output -------------------------------------------------------------

try:
    import aiohttp_theme  # noqa: F401

    html_theme = "aiohttp_theme"
    html_theme_options = {
        "description": "Mock aiohttp requests through a real test server",
        "canonical_url": "https://aiointercept.readthedocs.io/en/latest/",
        "github_user": "Polandia94",
        "github_repo": "aiointercept",
        "github_button": True,
        "github_type": "star",
        "github_banner": True,
        "badges": [
            {
                "image": "https://img.shields.io/pypi/v/aiointercept.svg",
                "target": "https://pypi.org/project/aiointercept/",
                "height": "20",
                "alt": "Latest PyPI package version",
            },
        ],
    }
    html_sidebars = {
        "**": [
            "about.html",
            "navigation.html",
            "searchbox.html",
        ]
    }
except ImportError:  # pragma: no cover - fall back to a built-in theme
    html_theme = "alabaster"

# No custom static assets yet; add html_static_path = ["_static"] if needed.
