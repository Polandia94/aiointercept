import typing

from multidict import MultiDict
from yarl import URL


def normalize_url(url: "URL | str") -> URL:
    """Normalize url to make comparisons."""
    url = URL(url)
    if url.fragment:
        url = url.with_fragment(None)
    sorted_query = sorted(url.query.items())
    # Normalize "http://host" and "http://host/" to the same key.
    if url.host:
        url = url.with_path(url.path or "/")
    return url.with_query(sorted_query)


def merge_params(url: URL | str, params: typing.Mapping[str, str] | None = None) -> URL:
    url = URL(url)
    if params:
        query_params = MultiDict(url.query)
        query_params.extend(url.with_query(params).query)
        return url.with_query(query_params)
    return url
