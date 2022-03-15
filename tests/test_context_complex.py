import asyncio

import pytest
from koil.errors import ContextError

from koil import unkoil, unkoilable, koilable
import koil
from koil.errors import CancelledError
from koil.helpers import iterate_spawned, run_spawned, unkoil_gen
from koil.vars import check_cancelled
import time
from koil import Koil


@koilable
class T(object):
    def __init__(self) -> None:
        pass

    @unkoilable
    async def a(self, a):
        await asyncio.sleep(1)
        return 5

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        pass


@koilable()
class X(object):
    def __init__(self, x):
        self.x = x

    def sleep_and_call(self, nana):
        time.sleep(0.04)
        y = self.a(nana, as_task=True).run()
        check_cancelled()
        time.sleep(0.04)
        return y.result()

    def sleep_and_yield(self, nana):
        for i in range(2):
            a = self.a("v")
            check_cancelled()
            t = yield a
            print(t)

    @unkoilable
    async def a(self, a):
        return a + "iterator"

    @unkoilable
    async def t(self):
        return "x" + await run_spawned(self.sleep_and_call, "haha", cancel_timeout=3)

    async def g(self):
        async for i in iterate_spawned(self.sleep_and_yield, "haha", cancel_timeout=3):
            x = yield i + "33"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        pass


async def test_async():

    async with Koil():
        async with X(1) as x:
            x = asyncio.create_task(x.t())
            await asyncio.sleep(0.02)
            x.cancel()
            try:
                x = await x
            except asyncio.CancelledError as e:
                pass


def test_x_sync():

    with X(1) as x:
        l = unkoil_gen(x.g)
        l.send(None)
        l.send(None)
        print("Here")
        try:
            l.send(None)
        except StopIteration:
            pass