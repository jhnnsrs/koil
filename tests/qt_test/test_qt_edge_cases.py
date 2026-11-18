"""The Qt bridge at its edges: cancellation, timeouts, ContextVars, deadlocks.

The wrappers are called from the thread they live in, so their slot runs at
once and no Qt event loop is needed.
"""

import asyncio
import contextvars
import threading

import pytest

# Skip the entire module unless a Qt binding is installed.
pytest.importorskip("PyQt5")

from koil import CancelledError, Koil, KoilError, unkoil  # noqa: E402
from koil.qt import (  # noqa: E402
    QtFuture,
    QtGenerator,
    qt_gen_to_async_gen,
    qt_to_async,
)

var: contextvars.ContextVar[str] = contextvars.ContextVar("qt_var", default="unset")


def test_cancelling_the_awaiting_task_tells_the_qt_side() -> None:
    async def main() -> tuple[bool, list[object], list[object]]:
        held: list[QtFuture[int]] = []
        wrapped = qt_to_async(lambda future: held.append(future))  # never resolves
        told: list[object] = []
        wrapped.cancelled.connect(told.append)
        from_future: list[object] = []

        task = asyncio.ensure_future(wrapped.acall())
        await asyncio.sleep(0.01)
        held[0].cancelled.connect(from_future.append)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return held[0].iscancelled, told, from_future

    iscancelled, told, from_future = asyncio.run(main())
    assert iscancelled
    assert len(told) == 1 and isinstance(told[0], QtFuture)
    assert len(from_future) == 1 and isinstance(from_future[0], CancelledError)


def test_a_timeout_tells_the_qt_side_too() -> None:
    async def main() -> QtFuture[int]:
        held: list[QtFuture[int]] = []
        wrapped = qt_to_async(lambda future: held.append(future), timeout=0.05)
        with pytest.raises(TimeoutError):
            await wrapped.acall()
        return held[0]

    assert asyncio.run(main()).iscancelled


def test_resolving_after_the_task_gave_up_is_ignored() -> None:
    async def main() -> None:
        held: list[QtFuture[int]] = []
        wrapped = qt_to_async(lambda future: held.append(future), timeout=0.05)
        with pytest.raises(TimeoutError):
            await wrapped.acall()
        held[0].resolve(1)  # the dialog was answered too late
        await asyncio.sleep(0.01)

    asyncio.run(main())


def test_what_the_slot_sets_reaches_the_awaiting_task() -> None:
    def slot(future: QtFuture[str]) -> None:
        seen = var.get()
        var.set("set by the slot")
        future.resolve(seen)

    async def main() -> tuple[str, str]:
        var.set("set by the task")
        seen = await qt_to_async(slot).acall()
        return seen, var.get()

    assert asyncio.run(main()) == ("set by the task", "set by the slot")


def test_a_future_resolved_from_elsewhere_brings_no_foreign_context() -> None:
    """Answered later, from another slot: that slot's ContextVars are not ours."""

    async def main() -> tuple[int, str]:
        held: list[QtFuture[int]] = []
        wrapped = qt_to_async(lambda future: held.append(future))
        var.set("the task's own")

        def unrelated_slot() -> None:
            var.set("someone else's")
            held[0].resolve(5)

        # Runs in a context of its own, as a slot of another call would.
        asyncio.get_running_loop().call_later(
            0.01, contextvars.copy_context().run, unrelated_slot
        )
        return await wrapped.acall(), var.get()

    assert asyncio.run(main()) == (5, "the task's own")


def test_generator_values_carry_only_what_their_slot_changed() -> None:
    def produce(generator: QtGenerator[int]) -> None:
        var.set("set while producing")
        generator.next(1)
        generator.next(2)
        generator.stop()

    async def main() -> tuple[list[int], str]:
        var.set("before")
        values = [value async for value in qt_gen_to_async_gen(produce).acall()]
        return values, var.get()

    assert asyncio.run(main()) == ([1, 2], "set while producing")


def test_a_slot_that_raises_fails_the_call() -> None:
    def slot(future: QtFuture[int]) -> None:
        raise ValueError("the slot failed")

    async def main() -> None:
        await qt_to_async(slot).acall()

    with pytest.raises(ValueError, match="the slot failed"):
        asyncio.run(main())


def test_blocking_the_qt_thread_on_a_call_that_needs_it_raises_instead_of_hanging() -> None:
    """``unkoil`` on the Qt thread, on a coroutine that waits for a slot there."""
    wrapped = qt_to_async(lambda future: future.resolve(1))

    async def needs_the_qt_thread() -> int:
        return await wrapped.acall()

    with Koil():
        with pytest.raises(KoilError, match="Deadlock"):
            unkoil(needs_the_qt_thread)


def test_another_thread_may_block_on_a_call_that_needs_the_qt_thread(qapp) -> None:
    """Only the thread the slot runs on is the problem."""
    wrapped = qt_to_async(lambda future: future.resolve(1))
    refused: list[BaseException] = []
    waiting = threading.Event()

    async def needs_the_qt_thread() -> int:
        waiting.set()
        return await asyncio.wait_for(wrapped.acall(), timeout=0.3)

    def body() -> None:
        try:
            unkoil(needs_the_qt_thread)
        except BaseException as e:
            refused.append(e)

    with Koil():
        thread = threading.Thread(target=contextvars.copy_context().run, args=(body,))
        thread.start()
        assert waiting.wait(5)
        thread.join(5)
        qapp.processEvents()  # deliver the slot now, not during a later test
    # No Qt loop ran while it waited, so the slot came too late and the call
    # timed out — but it was allowed to wait, not refused as a deadlock.
    assert len(refused) == 1 and isinstance(refused[0], TimeoutError)


def test_a_task_spawned_by_the_blocking_call_is_not_refused(qapp) -> None:
    """Nobody is blocked on a background task, even one started under ``unkoil``."""
    wrapped = qt_to_async(lambda future: future.resolve(1))
    outcome: list[object] = []

    async def background() -> None:
        try:
            await asyncio.wait_for(wrapped.acall(), timeout=0.1)
        except BaseException as e:
            outcome.append(e)

    async def start() -> None:
        task = asyncio.ensure_future(background())
        await task

    with Koil():
        unkoil(start)
        qapp.processEvents()
    assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)


def test_an_answer_for_a_loop_that_is_gone_is_dropped() -> None:
    async def make() -> QtFuture[int]:
        return QtFuture()

    future = asyncio.run(make())  # its loop is closed by now
    future.resolve(1)
    future.reject(ValueError("too late"))
