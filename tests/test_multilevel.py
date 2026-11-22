import asyncio

import pytest

from koil import Koil
from koil.bridge import unkoil
from koil.composition.base import KoiledModel


class Kant(KoiledModel):
    connected: bool = False

    async def __aenter__(self) -> "Kant":
        await asyncio.sleep(0.004)

        self.connected = True
        return self

    async def __aexit__(self, *args: object, **kwargs: object) -> None:
        self.connected = False


class Tan(KoiledModel):  #
    x: int = 3

    async def arun(self) -> int:
        await asyncio.sleep(0.02)
        return self.x

    def run(self) -> int:
        return unkoil(self.arun)

    async def __aenter__(self) -> "Tan":
        await asyncio.sleep(0.002)
        self.x = 4
        return self

    async def __aexit__(self, *args: object, **kwargs: object) -> None:
        pass


def test_multilevel() -> None:
    with Koil():
        with Tan() as t:
            assert t.x == 4, "tan.x should be 4 because it was set in enter"

        with Kant() as k:
            assert k.connected, "kant should be connected"


@pytest.mark.asyncio
async def test_nothing_multilevel() -> None:
    async with Koil():
        async with Tan() as t:
            assert t.x == 4, "tan.x should be 4 because it was set in enter"

        async with Kant() as k:
            assert k.connected, "kant should be connected"
