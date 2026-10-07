"""What a QtGenerator hands to the loop that consumes it."""

import asyncio

import pytest

# Skip the entire module unless a Qt binding is installed.
pytest.importorskip("PyQt5")

from koil.qt import QtGenerator, QtStopIteration, qt_gen_to_async_gen  # noqa: E402


def test_every_value_is_yielded_once_and_stop_ends_it() -> None:
    """Values handed over before the loop takes the first are all delivered, in order."""

    async def consume() -> list[int]:
        generator: QtGenerator[int] = QtGenerator()
        generator.next(1)
        generator.next(2)
        generator.next(3)
        generator.stop()
        taken = []
        with pytest.raises(QtStopIteration):
            while True:
                _, value = await asyncio.wait_for(generator.anext(), timeout=1)
                taken.append(value)
        return taken

    assert asyncio.run(consume()) == [1, 2, 3]


def test_a_thrown_exception_reaches_the_consumer() -> None:
    """What Qt throws ends the generator with that exception, after what came before."""

    async def consume() -> list[int]:
        generator: QtGenerator[int] = QtGenerator()
        generator.next(1)
        generator.throw(ValueError("no more"))
        taken = [(await generator.anext())[1]]
        with pytest.raises(ValueError, match="no more"):
            await generator.anext()
        return taken

    assert asyncio.run(consume()) == [1]


def test_a_stopped_generator_ends_the_iteration_it_feeds() -> None:
    """``stop()`` ends the ``async for`` over the call; it is not an error of it."""

    async def consume() -> list[int]:
        def produce(generator: QtGenerator[int], upto: int) -> None:
            for value in range(upto):
                generator.next(value)
            generator.stop()

        # Called from the thread the wrapper lives in, so the slot runs at once
        # and no Qt loop is needed.
        wrapped = qt_gen_to_async_gen(produce)
        return [value async for value in wrapped.acall(3)]

    assert asyncio.run(consume()) == [0, 1, 2]
