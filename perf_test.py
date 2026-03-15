import asyncio
import time
from aiohttp import ClientSession
from poc import aioresponses_2
from aioresponses import aioresponses

class BusinessLogic:
    async def do_something(self):
        async with ClientSession() as session:
            resp = await session.get("http://example.com/foo", params={"bar": "baz"})
            return resp.status, await resp.text()

async def run_main(iters):
    """ run the test using the original aioresponses implementation """
    for _ in range(iters):
        with aioresponses() as aio:
            aio.get("http://example.com/foo?bar=baz", status=200, body="Mocked response")
            business_logic = BusinessLogic()
            status, body = await business_logic.do_something()
            aio.assert_called_with("http://example.com/foo", method="GET", params={"bar": "baz"})

async def run_main2(iters):
    """ run the same test but using the POC implementation """
    for _ in range(iters):
        async with aioresponses_2() as aio:
            aio.get("http://example.com/foo?bar=baz", status=200, body="Mocked response")
            business_logic = BusinessLogic()
            status, body = await business_logic.do_something()
            aio.assert_called_with("http://example.com/foo", method="GET", params={"bar": "baz"})

async def measure():
    iters = 1000
    
    # Warmup
    await run_main(1)
    await run_main2(1)
    
    t0 = time.perf_counter()
    await run_main(iters)
    t1 = time.perf_counter()
    time_main = t1 - t0
    
    t0 = time.perf_counter()
    await run_main2(iters)
    t1 = time.perf_counter()
    time_main2 = t1 - t0

    
    print(f"aioresponses (main):          {time_main:.4f} seconds for {iters} iterations ({time_main/iters*1000:.2f} ms/iter)")
    print(f"aioresponses_2 (POC):         {time_main2:.4f} seconds for {iters} iterations ({time_main2/iters*1000:.2f} ms/iter)")

if __name__ == "__main__":
    asyncio.run(measure())

# aioresponses (main)       :  0.7223 seconds for 1000 iter (0.72 ms/iter)
# aioresponses_2 (POC)      :  0.8272 seconds for 1000 iter (0.83 ms/iter)
