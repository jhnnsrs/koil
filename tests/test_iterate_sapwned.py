import asyncio
from koil.helpers import iterate_spawned
from .context import AsyncContextManager
from koil import Koil
import time


async def sleep(ins: int) -> int:
    await asyncio.sleep(0.001)
    return ins


def iterating():
    yield 1
    time.sleep(0.001)
    yield 2
    time.sleep(0.001)
    yield 3


async def test_iterating():
    async with Koil():
        t = iterate_spawned(iterating)
        assert await anext(t) == 1
        assert await anext(t) == 2
        assert await anext(t) == 3
        try:
            await anext(t)
        except StopAsyncIteration:
            pass
        else:
            assert False, "Should have raised StopAsyncIteration"
