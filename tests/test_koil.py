import asyncio
from koil import koiled
from koil.koil import Koil
from koil.loop import koil
import pytest


async def sleep(ins):
    await asyncio.sleep(0.1)
    return ins


def test_sync():
    
    with koiled as koils:
        assert koil(sleep(1), koil=koils) == 1, "Koil realized its async and was okay with that"


async def test_async():

    async with koiled as koils:
        assert await koil(sleep(1), koil=koils) == 1, "Koil realized its async and was okay with that"



async def test_async_sync():
    
    with koiled as koils:
        assert koil(sleep(1), koil=koils) == 1, "Koil realized its async and was okay with that"