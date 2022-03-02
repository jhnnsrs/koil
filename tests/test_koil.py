import asyncio

import pytest
from koil.errors import ContextError

from koil.helpers import unkoil
from .context import AsyncContextManager
from koil import Koil


async def sleep(ins):
    await asyncio.sleep(0.001)
    return ins


def test_sync_context():

    with AsyncContextManager() as c:
        print(c.aprint())


async def test_async_context():
    async with AsyncContextManager() as c:
        print(await c.aprint())


def test_sync():

    with Koil():
        assert unkoil(sleep, 1) == 1, "Koil realized its async and was okay with that"


async def test_async():

    async with Koil():
        assert (
            await unkoil(sleep, 1) == 1
        ), "Koil realized its async and was okay with that"


async def test_async_sync():

    with pytest.raises(ContextError):
        with Koil():
            assert (
                unkoil(sleep(1)) == 1
            ), "Koil realized its async and was okay with that"


def test_double_context():

    with Koil():

        with AsyncContextManager() as c:
            print(c.aprint())

        with AsyncContextManager() as c:
            print(c.aprint())
