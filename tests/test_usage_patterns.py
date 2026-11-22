"""Koil the way the packages built on it use it.

* API clients are ``KoiledModel``/``Composition`` objects whose sync methods
  are one-line ``unkoil``/``unkoil_gen`` wrappers around async ones (rath,
  mikro, fakts, ...), used from scripts inside ``with client:``.
* The agent runtime runs a user's *sync* action in a worker
  (``run_threaded`` for a function, ``iterate_threaded`` for a generator);
  that action calls the sync client methods, sleeps with ``koil.sleep`` and is
  cancelled by cancelling the task that awaits it.
* A test harness starts the agent with ``unkoil_task`` and cancels the future
  when the test is over.
"""

import asyncio
import contextvars
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any

import pytest

import koil
from koil import (
    Koil,
    ThreadCancelledError,
    check_cancelled,
    iterate_threaded,
    run_threaded,
    shielded,
    unkoil,
    unkoil_gen,
    unkoil_task,
)
from koil.composition import Composition, KoiledModel

current_assignment: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_assignment", default=None
)


class Api(KoiledModel):
    """A client: async methods, and sync twins that bridge to them."""

    connected: bool = False
    closed: bool = False

    async def __aenter__(self) -> "Api":
        self.connected = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.closed = True

    async def aquery(self, value: int) -> int:
        await asyncio.sleep(0.001)
        return value * 2

    def query(self, value: int) -> int:
        return unkoil(self.aquery, value)

    async def asubscribe(self, count: int) -> AsyncGenerator[int, None]:
        for i in range(count):
            await asyncio.sleep(0.001)
            yield i

    def subscribe(self, count: int) -> Generator[int, None, None]:
        return unkoil_gen(self.asubscribe, count)

    async def awho(self) -> str | None:
        return current_assignment.get()

    def who(self) -> str | None:
        return unkoil(self.awho)


class App(Composition):
    api: Api
    storage: Api


# The two strategies the runtime's functional actor uses for sync actions.


async def threaded_func(
    assign: Callable[..., Any], **params: Any
) -> AsyncGenerator[Any, None]:
    yield await run_threaded(assign, **params)


async def threaded_gen(
    assign: Callable[..., Any], **params: Any
) -> AsyncGenerator[Any, None]:
    async for returns in iterate_threaded(assign, **params):
        yield returns


async def collect(
    strategy: Callable[..., Any], assign: Callable[..., Any], **params: Any
) -> list[Any]:
    return [returns async for returns in strategy(assign, **params)]


async def _in_thread(fn: Callable[[], Any]) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, fn)


# -- scripts ----------------------------------------------------------------------


def test_a_script_uses_the_sync_methods_inside_the_with_block() -> None:
    app = App(api=Api(), storage=Api())
    with app:
        assert app.api.connected and app.storage.connected
        assert app.api.query(21) == 42
        assert list(app.api.subscribe(3)) == [0, 1, 2]
    assert app.api.closed and app.storage.closed


def test_clients_entered_under_one_koil_share_its_loop() -> None:
    async def running_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    with Koil():
        with Api() as first, Api() as second:
            assert first.query(1) == 2 and second.query(2) == 4
            assert unkoil(running_loop) is unkoil(running_loop)


def test_the_async_methods_are_used_directly_from_async_code() -> None:
    async def main() -> int:
        async with App(api=Api(), storage=Api()) as app:
            return await app.api.aquery(4)

    assert asyncio.run(main()) == 8


def test_the_deprecated_helpers_module_still_bridges() -> None:
    with pytest.warns(DeprecationWarning):
        from koil.helpers import unkoil as old_unkoil

    with Api() as api:
        assert old_unkoil(api.aquery, 2) == 4


# -- sync actions run by the agent ------------------------------------------------


def test_a_sync_action_calls_the_client_from_its_worker() -> None:
    app = App(api=Api(), storage=Api())

    def action(value: int) -> int:
        return app.api.query(value) + sum(app.storage.subscribe(4))

    with app:
        assert unkoil(collect, threaded_func, action, value=5) == [16]


def test_a_sync_generator_action_yields_through_the_bridge() -> None:
    app = App(api=Api(), storage=Api())

    def action(count: int) -> Generator[int, None, None]:
        for i in app.api.subscribe(count):
            yield app.api.query(i)

    with app:
        assert unkoil(collect, threaded_gen, action, count=4) == [0, 2, 4, 6]


def test_many_assignments_at_once_stay_apart() -> None:
    """Each assignment's context reaches its worker, and the calls that worker makes."""
    api = Api()

    def action(value: int) -> tuple[str | None, int]:
        seen = {api.who() for _ in range(3)}
        assert len(seen) == 1
        return seen.pop(), api.query(value)

    async def assign(i: int) -> tuple[str | None, int]:
        current_assignment.set(f"assignment-{i}")
        return await run_threaded(action, i)

    async def main() -> list[tuple[str | None, int]]:
        return list(await asyncio.gather(*(assign(i) for i in range(50))))

    with api:
        results = unkoil(main)
    assert results == [(f"assignment-{i}", i * 2) for i in range(50)]


def test_cancelling_a_sleeping_action_stops_it_promptly() -> None:
    api = Api()
    events: list[str] = []
    asleep = threading.Event()

    def action() -> None:
        try:
            while True:
                asleep.set()
                koil.sleep(5)
        except ThreadCancelledError:
            events.append("cancelled")
            raise
        finally:
            with shielded():
                events.append(f"cleanup {api.query(1)}")

    async def main() -> float:
        task = asyncio.ensure_future(collect(threaded_func, action))
        await _in_thread(asleep.wait)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    with api:
        assert unkoil(main) < 1
    assert events == ["cancelled", "cleanup 2"]


def test_cancelling_a_generator_action_runs_its_finally_in_a_worker() -> None:
    finished: list[str] = []
    yielded = threading.Event()

    def action() -> Generator[int, None, None]:
        try:
            i = 0
            while True:
                check_cancelled()
                yield i
                i += 1
        finally:
            finished.append(threading.current_thread().name)

    async def main() -> list[int]:
        received: list[int] = []

        async def consume() -> None:
            async for value in threaded_gen(action):
                received.append(value)
                if len(received) == 3:
                    yielded.set()
                    await asyncio.sleep(30)  # cancelled while holding a value

        task = asyncio.ensure_future(consume())
        await _in_thread(yielded.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)  # let the abandoned generators be finalized
        return received

    with Koil():
        assert unkoil(main) == [0, 1, 2]
        assert len(finished) == 1 and finished[0].startswith("KoilWorker")


def test_stopping_early_closes_a_generator_action() -> None:
    closed = threading.Event()

    def action() -> Generator[int, None, None]:
        try:
            yield 1
            yield 2
        finally:
            closed.set()

    async def main() -> int:
        stream = iterate_threaded(action)
        first = await stream.__anext__()
        await stream.aclose()
        return first

    with Koil():
        assert unkoil(main) == 1
        assert closed.is_set()


def test_an_actions_error_reaches_the_agent_with_its_traceback() -> None:
    def action() -> None:
        raise ValueError("the action failed")

    with Koil():
        with pytest.raises(ValueError, match="the action failed") as excinfo:
            unkoil(collect, threaded_func, action)
    assert "action" in [frame.name for frame in excinfo.traceback]


# -- the agent run detached ---------------------------------------------------------


def test_a_detached_agent_is_cancelled_when_the_test_is_over() -> None:
    """``serving = unkoil_task(runtime.aloop)`` ... ``serving.cancel()``."""
    state: dict[str, bool] = {}
    serving_started = threading.Event()

    async def aloop() -> None:
        state["serving"] = True
        serving_started.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        finally:
            state["stopped"] = True

    started = time.monotonic()
    with Api() as api:
        serving = unkoil_task(aloop)
        try:
            assert serving_started.wait(5)
            assert api.query(3) == 6  # the app is usable while the agent serves
        finally:
            serving.cancel()
    assert state == {"serving": True, "stopped": True}
    assert time.monotonic() - started < 2
    assert serving.cancelled()


def test_a_detached_agents_crash_is_raised_by_its_future() -> None:
    async def aloop() -> None:
        raise ConnectionError("the agent lost its socket")

    with Koil():
        serving = unkoil_task(aloop)
        with pytest.raises(ConnectionError, match="lost its socket"):
            serving.result(timeout=5)
        assert serving.done()
