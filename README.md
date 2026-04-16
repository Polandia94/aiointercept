Proof of concept for an aioresponses-like library that uses real aiohttp server
related to this discussion: https://github.com/orgs/aio-libs/discussions/45

This is a proof of concept of how a linrary could expose aioresponses-like API
while using real aiohttp server under the hood. It works only with GET requests,
only compare the url, param and method, and only implements assert_called_with.

The breaking changes will be:
- The context manager will be async, as it will need to start the server
- the assert_called_* methods will not work with arbitrary kwargs, instead they will only work with specific args that the request was made with (e.g. url, method, headers, params, etc.)
- the fragments (after #) are not being passed on the request, so will not be compared
- on a connector exception, aiohttp could retry, so the number of request will not be the same
- pass timeout don't work
- you cant raise exceptions exception clienterror
- the decorathor need to decorate an async function
- as this mock DNS will not work to mock request to external IPs


Added a perf test to compare the performance of the POC with the original aioresponses implementation.

Results:

aioresponses (main)       :  0.7223 seconds for 1000 iter (0.72 ms/iter)
aioresponses_2 (POC)      :  0.8272 seconds for 1000 iter (0.83 ms/iter)

Probablly this overheard is tolerable, and could be optimized.
However, once we start adding more features, the performance will degrade.


Changing:

@pytest.fixture
def mock_aioresponse():
    with aioresponses() as m:

to:

@pytest_asyncio.fixture
async def mock_aioresponse():
    async with aioresponses() as m:
        yield m

is passing the tests on:

https://github.com/cortega26/PDF-Text-Analyzer
https://github.com/mxr/reconciler-for-ynab
https://github.com/pratik-choudhari/Financial-news-scraper /with some unrelated changes because is broken
https://github.com/dorianrod/GitReviewLens

https://github.com/natekspencer/pylitterbot required some changes:
- there were raising a custom exception on request on test_litter_robot_5_dispatch_command_failure, and that is not supported.
- - However, they were try to mimick a 500 with certain json that was supported
- There were some calls to localhost, that should be to https://localhost. This was
also a fixable error on the tests


Broken on main
https://github.com/symphony-youri/symphony-api-client-python.git

too complex to work: https://github.com/mguidon/osparc-simcore