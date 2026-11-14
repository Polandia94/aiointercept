import pytest
from yarl import URL

from aiointercept.compat import merge_params, normalize_url

URL_WITH_PARAMETERS = "http://example.com/api?foo=bar#fragment"
URL_WITHOUT_PARAMETERS = "http://example.com/api?#fragment"

as_str_param = pytest.mark.parametrize("as_str", [True, False])


def get_url(url: str, as_str: bool) -> URL | str:
    return url if as_str else URL(url)


@as_str_param
def test_no_params_returns_same_url(as_str):
    url = get_url(URL_WITH_PARAMETERS, as_str)
    assert merge_params(url, None) == URL(URL_WITH_PARAMETERS)


@as_str_param
def test_empty_params_returns_same_url(as_str):
    url = get_url(URL_WITH_PARAMETERS, as_str)
    assert merge_params(url, {}) == URL(URL_WITH_PARAMETERS)


@as_str_param
def test_both_with_params_returns_corrected_url(as_str):
    url = get_url(URL_WITH_PARAMETERS, as_str)
    assert merge_params(url, {"x": 42}) == URL("http://example.com/api?foo=bar&x=42#fragment")


@as_str_param
def test_base_without_params_returns_corrected_url(as_str):
    url = get_url(URL_WITHOUT_PARAMETERS, as_str)
    assert merge_params(url, {"x": 42}) == URL("http://example.com/api?x=42#fragment")


@as_str_param
@pytest.mark.parametrize(
    ("original_url", "expected_url"),
    [
        pytest.param(
            "http://example.com/api?var2=baz&var1=foo",
            "http://example.com/api?var1=foo&var2=baz",
            id="trivial",
        ),
        pytest.param(
            "https://example.com/api?var3=gaz%3Bdar&var1=foo:bar&var1=bar/baz&var2=baz%26gaz",
            "https://example.com/api?var1=bar/baz&var1=foo:bar&var2=baz%26gaz&var3=gaz%3Bdar",
            id="multi-occurrence-keys",
        ),
        pytest.param(
            "https://example.com/api?var3=gaz%3Bdar&var1=foo%3Abar&var1=bar%2Fbaz&var2=baz%26gaz",
            "https://example.com/api?var1=bar/baz&var1=foo:bar&var2=baz%26gaz&var3=gaz%3Bdar",
            id="multi-occurrence-keys-pre-encoded",
        ),
        pytest.param(
            "https://example.com/api?var=путь",
            "https://example.com/api?var=%D0%BF%D1%83%D1%82%D1%8C",
            id="non-ascii",
        ),
        pytest.param(
            "https://example.com/api?var=%D0%BF%D1%83%D1%82%D1%8C",
            "https://example.com/api?var=%D0%BF%D1%83%D1%82%D1%8C",
            id="non-ascii-pre-encoded",
        ),
    ],
)
def test_normalize_url(original_url, expected_url, as_str):
    received_url = normalize_url(get_url(original_url, as_str))
    assert isinstance(received_url, URL)
    assert expected_url == str(received_url)
