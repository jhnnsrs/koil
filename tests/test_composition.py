from koil.composition import Composition
import asyncio
from pydantic import Field
from koil.composition.base import KoiledModel

from koil.helpers import unkoil


class Kant(KoiledModel):
    connected: bool = False

    async def __aenter__(self):
        self.connected = True
        await asyncio.sleep(0.004)
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


class App(Composition):
    kant: Kant = Field(default_factory=Kant)
    tan: Tan = Field(default_factory=Tan)


def test_composition_api_sync():
    app = App()

    assert app.tan.x == 3, "tan.x should be 3"
    with app:
        assert app.kant.connected, "kant should be connected"
        assert app.tan.run() == 4, "tan.x should be 4 because it was set in enter"

    assert not app.kant.connected, "kant should be disconnected"


async def test_composition_api_async():
    app = App()

    assert app.tan.x == 3, "tan.x should be 3"
    async with app:
        assert app.kant.connected, "kant should be connected"
        assert await app.tan.arun() == 4, (
            "tan.x should be 4 because it was set in enter"
        )

    assert not app.kant.connected, "kant should be disconnected"
