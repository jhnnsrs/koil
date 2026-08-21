from typing import Any

from koil.composition import Composition
import asyncio
from pydantic import Field
from koil.composition.base import KoiledModel
from koil.loop import Koil
from koil.bridge import unkoil


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



def test_multiple_contexts():
    
    app = App()

    with Koil():
        
        
        assert app.tan.x == 3, "tan.x should be 3"
       
        with app as app:
            assert app.kant.connected, "kant should be connected"
            assert app.tan.run() == 4, "tan.x should be 4 because it was set in enter"
    
        with app as app:
            assert app.tan.x == 4, "tan.x should be 3"
            assert app.kant.connected, "kant should be connected"
            assert app.tan.run() == 4, "tan.x should be 4 because it was set in enter"
    

class _Recorder(KoiledModel):
    """Child that records enter/exit events into a shared journal."""

    name: str
    journal: Any
    fail_on_enter: bool = False
    fail_on_exit: bool = False

    async def __aenter__(self):
        if self.fail_on_enter:
            raise RuntimeError(f"{self.name} failed to enter")
        self.journal.append(("enter", self.name))
        return self

    async def __aexit__(self, *args, **kwargs):
        self.journal.append(("exit", self.name))
        if self.fail_on_exit:
            raise RuntimeError(f"{self.name} failed to exit")


class _RecorderApp(Composition):
    a: _Recorder
    b: _Recorder
    c: _Recorder


async def test_composition_exits_in_reverse_order():
    journal: list = []
    app = _RecorderApp(
        a=_Recorder(name="a", journal=journal),
        b=_Recorder(name="b", journal=journal),
        c=_Recorder(name="c", journal=journal),
    )
    async with app:
        pass
    assert journal == [
        ("enter", "a"),
        ("enter", "b"),
        ("enter", "c"),
        ("exit", "c"),
        ("exit", "b"),
        ("exit", "a"),
    ]


async def test_composition_failed_enter_unwinds_entered_children():
    journal: list = []
    app = _RecorderApp(
        a=_Recorder(name="a", journal=journal),
        b=_Recorder(name="b", journal=journal, fail_on_enter=True),
        c=_Recorder(name="c", journal=journal),
    )
    import pytest

    with pytest.raises(RuntimeError, match="b failed to enter"):
        async with app:
            pass
    # a was entered before b failed, and must have been exited again;
    # c was never entered so never exited.
    assert journal == [("enter", "a"), ("exit", "a")]


async def test_composition_failing_exit_still_exits_remaining_children():
    journal: list = []
    app = _RecorderApp(
        a=_Recorder(name="a", journal=journal),
        b=_Recorder(name="b", journal=journal, fail_on_exit=True),
        c=_Recorder(name="c", journal=journal),
    )
    import pytest

    with pytest.raises(RuntimeError, match="b failed to exit"):
        async with app:
            pass
    # All three exits ran (reverse order) even though b's exit raised.
    assert [e for e in journal if e[0] == "exit"] == [
        ("exit", "c"),
        ("exit", "b"),
        ("exit", "a"),
    ]
