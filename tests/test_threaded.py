import asyncio
from collections.abc import AsyncGenerator

from koil import Koil
from koil.bridge import unkoil, unkoil_gen

from .context import AsyncContextManager


async def sleep(ins: int) -> int:
    await asyncio.sleep(0.001)
    return ins


async def iterating() -> AsyncGenerator[int, None]:
    yield 1
    await asyncio.sleep(0.001)
    yield 2
    await asyncio.sleep(0.001)
    yield 3


def test_sync_context() -> None:
    with AsyncContextManager() as c:
        assert unkoil(c.aprint) == "sss", (
            "Koil realized its async and was okay with that"
        )


async def test_async_context() -> None:
    async with AsyncContextManager() as c:
        print(await c.aprint())


def test_sync() -> None:
    with Koil():
        assert unkoil(sleep, 1) == 1, "Koil realized its async and was okay with that"


async def test_async() -> None:
    async with Koil():
        assert await sleep(1) == 1, "Koil realized its async and was okay with that"


async def test_async_sync() -> None:
    with Koil():
        assert unkoil(sleep, 1) == 1, "Koil realized its async and was okay with that"


def test_iterating() -> None:
    with Koil():
        x = unkoil_gen(iterating)
        assert next(x) == 1
        assert next(x) == 2
        assert next(x) == 3

        try:
            next(x)
        except StopIteration:
            pass
        else:
            raise AssertionError("Should have raised StopIteration")
