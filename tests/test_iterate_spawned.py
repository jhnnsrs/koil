import asyncio
import time
from collections.abc import Generator

import pytest

from koil import Koil
from koil.bridge import iterate_threaded


async def sleep(ins: int) -> int:
    await asyncio.sleep(0.001)
    return ins


def iterating() -> Generator[int, None, None]:
    yield 1
    time.sleep(0.001)
    yield 2
    time.sleep(0.001)
    yield 3


async def test_iterating() -> None:
    async with Koil():
        t = iterate_threaded(iterating)
        assert await anext(t) == 1
        assert await anext(t) == 2
        assert await anext(t) == 3
        try:
            await anext(t)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("Should have raised StopAsyncIteration")


def echoing() -> Generator[int, int, None]:
    val = yield 1
    val = yield val + 1
    yield val + 1


async def test_iterate_spawned_send() -> None:
    """Values sent into the async generator are delivered into the sync generator."""
    async with Koil():
        agen = iterate_threaded(echoing)
        assert await agen.asend(None) == 1
        assert await agen.asend(10) == 11
        assert await agen.asend(20) == 21
        with pytest.raises(StopAsyncIteration):
            await agen.asend(None)


async def test_iterate_spawned_closes_generator_on_early_exit() -> None:
    """Closing the async generator early runs the sync generator's finally block."""
    cleaned: list[bool] = []

    def gen() -> Generator[int, None, None]:
        try:
            yield 1
            yield 2
        finally:
            cleaned.append(True)

    async with Koil():
        agen = iterate_threaded(gen)
        assert await anext(agen) == 1
        await agen.aclose()

    assert cleaned == [True]
