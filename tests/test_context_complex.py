import asyncio
from typing import AsyncGenerator


from koil import unkoil, koilable
from koil.composition.base import KoiledModel
from koil.helpers import iterate_spawned, run_spawned, unkoil_gen, unkoil_task
from koil.koil import Koil
from koil.vars import check_cancelled
import time


@koilable()
class T(object):
    def __init__(self) -> None:
        pass

    async def a(self, a):
        await asyncio.sleep(1)
        return 5

    def t(self, a):
        return unkoil(self.a, a)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        pass


class X(KoiledModel):
    x: int

    def sleep_and_call(self, nana: str) -> str:
        time.sleep(0.04)
        y = unkoil_task(self.a, nana)
        check_cancelled()
        time.sleep(0.04)
        return y.result()

    def sleep_and_yield(self, nana: str):
        for i in range(2):
            a = unkoil(self.a, "v")
            check_cancelled()
            yield a

    async def a(self, a: str) -> str:
        return a + "iterator"

    async def t(self):
        f = await run_spawned(self.sleep_and_call, "haha")
        return "x" + f

    async def g(self) -> AsyncGenerator[str, None]:
        async for elem in iterate_spawned(self.sleep_and_yield, "haha"):
            yield elem + "33"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        pass


async def test_async():
    async with X(x=1) as x:
        x = asyncio.create_task(x.t())
        await asyncio.sleep(0.02)
        x.cancel()
        try:
            x = await x
        except asyncio.CancelledError:
            pass


def test_x_sync():
    with X(x=1) as x:
        sender = unkoil_gen(x.g)
        sender.send(None)
        sender.send(None)
        try:
            sender.send(None)
        except StopIteration:
            pass
