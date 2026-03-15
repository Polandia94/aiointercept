import asyncio
from aiohttp import ClientSession
from poc import aioresponses_2


class BusinessLogic:
    async def do_something(self):
        async with ClientSession() as session:
            resp = await session.get("http://example.com/foo", params={"bar": "baz"})
            return resp.status, await resp.text()


async def main():
    async with aioresponses_2() as aio:
        aio.get("http://example.com/foo", status=200, body="Mocked response")

        business_logic = BusinessLogic()
        status, body = await business_logic.do_something()

        print(f"Status: {status}")  # Status: 200
        print(f"Body: {body}")  # Body: Mocked response
        aio.assert_called_with(
            "http://example.com/foo", method="GET", params={"bar": "baz"}
        )


asyncio.run(main())
