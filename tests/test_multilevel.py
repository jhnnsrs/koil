from koil.composition import Composition
import asyncio
from pydantic import Field
from koil.composition.base import KoiledModel
from koil import Koil
from koil.helpers import unkoil
import pytest


class Kant(KoiledModel):
    connected: bool = False

    async def __aenter__(self):
        await asyncio.sleep(0.004)

        self.connected = True
        return self

    async def __aexit__(self, *args, **kwargs):
        self.connected = False


class Tan(KoiledModel):  #
    x: int = 3

    async def arun(self):
        await asyncio.sleep(0.02)
        return self.x

    def run(self):
        return unkoil(self.arun)

    async def __aenter__(self):
        await asyncio.sleep(0.002)
        self.x = 4
        return self

    async def __aexit__(self, *args, **kwargs):
        pass


def test_multilevel():
    with Koil():
        with Tan() as t:
            assert t.x == 4, "tan.x should be 4 because it was set in enter"

        with Kant() as k:
            assert k.connected, "kant should be connected"


@pytest.mark.asyncio
async def test_nothing_multilevel():
    async with Koil():
        async with Tan() as t:
            assert t.x == 4, "tan.x should be 4 because it was set in enter"

        async with Kant() as k:
            assert k.connected, "kant should be connected"
