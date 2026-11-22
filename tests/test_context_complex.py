import asyncio
import time
from collections.abc import AsyncGenerator, Generator

from koil import koilable, unkoil
from koil.bridge import iterate_threaded, run_threaded, unkoil_gen, unkoil_task
from koil.composition.base import KoiledModel
from koil.context import check_cancelled


@koilable()
class T:
    def __init__(self) -> None:
        pass

    async def a(self, a: int) -> int:
        await asyncio.sleep(1)
        return 5

    def t(self, a: int) -> int:
        return unkoil(self.a, a)

    async def __aenter__(self) -> "T":
        return self

    async def __aexit__(self, *args: object, **kwargs: object) -> None:
        pass


class X(KoiledModel):
    x: int

    def sleep_and_call(self, nana: str) -> str:
        time.sleep(0.04)
        y = unkoil_task(self.a, nana)
        check_cancelled()
        time.sleep(0.04)
        return y.result()

    def sleep_and_yield(self, nana: str) -> Generator[str, None, None]:
        for _ in range(2):
            a = unkoil(self.a, "v")
            check_cancelled()
            yield a

    async def a(self, a: str) -> str:
        return a + "iterator"

    async def t(self) -> str:
        f = await run_threaded(self.sleep_and_call, "haha")
        return "x" + f

    async def g(self) -> AsyncGenerator[str, None]:
        async for elem in iterate_threaded(self.sleep_and_yield, "haha"):
            yield elem + "33"

    async def __aenter__(self) -> "X":
        return self

    async def __aexit__(self, *args: object, **kwargs: object) -> None:
        pass


async def test_async() -> None:

    async with X(x=1) as x:
        x = asyncio.create_task(x.t())
        await asyncio.sleep(0.02)
        x.cancel()
        try:
            x = await x
        except asyncio.CancelledError:
            pass


def test_x_sync() -> None:
    with X(x=1) as x:
        sender = unkoil_gen(x.g)
        sender.send(None)
        sender.send(None)
        try:
            sender.send(None)
        except StopIteration:
            pass
