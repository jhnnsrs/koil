from koil.composition import Composition
import asyncio
from koil import koilable, unkoilable
from pydantic import BaseModel, Field


@koilable()
class Kant(BaseModel):
    connected: bool = False

    async def __aenter__(self):
        self.connected = True
        await asyncio.sleep(0.004)
        pass

    async def __aexit__(self, *args, **kwargs):
        self.connected = False


@koilable()
class Tan(BaseModel):  #
    x: int = 3

    @unkoilable
    async def run(self):
        await asyncio.sleep(0.02)
        return self.x

    async def __aenter__(self):
        await asyncio.sleep(0.002)
        self.x = 4

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
        assert await app.tan.run() == 4, "tan.x should be 4 because it was set in enter"

    assert not app.kant.connected, "kant should be disconnected"
