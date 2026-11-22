"""Which exception a cancelled bridge call raises, what it leaves intact, and
how cleanup still gets to run.

* cancelling a :class:`~koil.KoilFuture` raises :class:`koil.CancelledError`;
* a call cut short because the *worker thread* making it was cancelled raises
  :class:`koil.ThreadCancelledError`, like every other check in that worker;
* a call the loop never got to finish is cancelled, not left hanging.
"""

import asyncio
import concurrent.futures
import contextvars
import os
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import pytest

import koil
from koil import (
    CancelledError,
    Koil,
    KoilError,
    KoilTimeoutError,
    ThreadCancelledError,
    check_cancelled,
    koilable,
    koiled_cm,
    run_threaded,
    shielded,
    unkoil,
    unkoil_task,
    unkoil_with_timeout,
)
from koil.context import current_cancel_event, global_koil, global_koil_loop

_T = TypeVar("_T")


async def _in_thread(fn: Callable[[], _T]) -> _T:
    return await asyncio.get_running_loop().run_in_executor(None, fn)


# -- cancelling a future -----------------------------------------------------


def test_a_cancelled_future_raises_koils_cancelled_error() -> None:
    with Koil():
        future = unkoil_task(asyncio.sleep, 30)
        assert future.cancel(reason="not needed") is True
        with pytest.raises(CancelledError) as excinfo:
            future.result()
        # What a cancelled bridge call used to raise; old handlers keep working.
        assert isinstance(excinfo.value, concurrent.futures.CancelledError)
        assert future.cancelled() and future.cancel_reason == "not needed"


def test_cancelling_one_future_leaves_the_others_running() -> None:
    async def answer() -> int:
        await asyncio.sleep(0.05)
        return 42

    with Koil():
        doomed = unkoil_task(asyncio.sleep, 30)
        kept = unkoil_task(answer)
        doomed.cancel()
        assert kept.result() == 42
        assert unkoil(answer) == 42  # and later calls are unaffected


def test_cancelling_a_finished_future_changes_nothing() -> None:
    async def answer() -> int:
        return 1

    with Koil():
        future = unkoil_task(answer)
        assert future.result() == 1
        assert future.cancel() is False
        assert future.result() == 1


def test_the_coroutine_sees_its_cancellation() -> None:
    unwound = threading.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            unwound.set()

    with Koil():
        future = unkoil_task(work)
        time.sleep(0.05)
        future.cancel()
        with pytest.raises(CancelledError):
            future.result()
        assert unwound.is_set()


def test_an_error_raised_while_unwinding_is_what_the_caller_gets() -> None:
    async def work() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise ValueError("cleanup went wrong") from None

    with Koil():
        future = unkoil_task(work)
        time.sleep(0.05)
        future.cancel()
        with pytest.raises(ValueError, match="cleanup went wrong"):
            future.result()


# -- a cancelled worker thread ----------------------------------------------


async def test_a_call_in_flight_raises_thread_cancelled_when_its_worker_is_cancelled() -> (
    None
):
    started = threading.Event()
    caught: list[BaseException] = []

    def work() -> None:
        started.set()
        try:
            unkoil(asyncio.sleep, 30)
        except BaseException as e:
            caught.append(e)
            raise

    task = asyncio.ensure_future(run_threaded(work))
    await _in_thread(started.wait)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [type(e) for e in caught] == [ThreadCancelledError]


async def test_handling_a_cancelled_future_inside_a_worker() -> None:
    def work() -> str:
        future = unkoil_task(asyncio.sleep, 30)
        future.cancel()
        try:
            future.result()
        except CancelledError:
            return "handled"
        return "not cancelled"

    assert await run_threaded(work) == "handled"


async def test_a_cancelled_future_escaping_a_worker_does_not_cancel_the_awaiting_task() -> (
    None
):
    """It is an error of that function, not a cancellation of whoever awaits it."""

    def work() -> None:
        future = unkoil_task(asyncio.sleep, 30)
        future.cancel()
        future.result()

    task = asyncio.ensure_future(run_threaded(work))
    with pytest.raises(CancelledError):
        await task
    assert not task.cancelled()


async def test_a_cancelled_worker_refuses_new_calls_unless_shielded() -> None:
    started = threading.Event()
    seen: dict[str, object] = {}

    async def aclose() -> str:
        await asyncio.sleep(0.01)
        return "closed"

    def work() -> None:
        started.set()
        try:
            koil.sleep(30)
        finally:
            try:
                unkoil(aclose)
            except ThreadCancelledError as e:
                seen["unshielded"] = e
            with shielded():
                check_cancelled()  # no longer raises
                seen["shielded"] = unkoil(aclose)
            try:
                check_cancelled()
            except ThreadCancelledError as e:
                seen["after"] = e

    task = asyncio.ensure_future(run_threaded(work))
    await _in_thread(started.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(seen["unshielded"], ThreadCancelledError)
    assert seen["shielded"] == "closed"
    assert isinstance(seen["after"], ThreadCancelledError)


class _Connection:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    async def __aenter__(self) -> "_Connection":
        self.opened = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.sleep(0.01)
        self.closed = True


async def test_a_koilables_exit_runs_in_a_cancelled_worker() -> None:
    connection = koilable(_Connection)()
    started = threading.Event()

    def work() -> None:
        with connection:
            started.set()
            koil.sleep(30)

    task = asyncio.ensure_future(run_threaded(work))
    await _in_thread(started.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.opened and connection.closed


async def test_a_koiled_cm_exit_runs_in_a_cancelled_worker() -> None:
    connection = _Connection()
    started = threading.Event()

    def work() -> None:
        with koiled_cm(connection):
            started.set()
            koil.sleep(30)

    task = asyncio.ensure_future(run_threaded(work))
    await _in_thread(started.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.closed


def test_shielded_restores_the_cancel_event() -> None:
    event = koil.context.KoilThreadSafeEvent()
    token = current_cancel_event.set(event)
    try:
        with shielded():
            assert current_cancel_event.get() is None
        assert current_cancel_event.get() is event
        with pytest.raises(RuntimeError):
            with shielded():
                raise RuntimeError("inside")
        assert current_cancel_event.get() is event
    finally:
        current_cancel_event.reset(token)


# -- the loop going away ------------------------------------------------------


def test_a_call_pending_when_the_loop_stops_is_cancelled() -> None:
    koil_instance = Koil()
    koil_instance.enter()
    future = unkoil_task(asyncio.sleep, 30)
    koil_instance.exit()
    with pytest.raises(CancelledError):
        future.result(timeout=5)


def test_a_thread_blocked_in_unkoil_is_released_when_the_loop_stops() -> None:
    outcome: list[BaseException | str] = []
    blocked = threading.Event()

    async def forever() -> None:
        blocked.set()
        await asyncio.Event().wait()

    def body() -> None:
        try:
            unkoil(forever)
            outcome.append("returned")
        except BaseException as e:
            outcome.append(e)

    with Koil():
        thread = threading.Thread(target=contextvars.copy_context().run, args=(body,))
        thread.start()
        assert blocked.wait(5)
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(outcome[0], CancelledError)


def test_calling_into_a_stopped_loop_raises_instead_of_hanging() -> None:
    with Koil():
        stale = contextvars.copy_context()

    with pytest.raises(RuntimeError):
        stale.run(unkoil, asyncio.sleep, 0)


def test_a_wedged_loop_cannot_hold_a_call_with_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is a timer on the loop; a loop that blocks never fires it."""
    monkeypatch.setattr(koil.utils, "TIMEOUT_BACKSTOP_MARGIN", 0.2)

    async def block_the_loop() -> None:
        time.sleep(1.5)  # noqa: ASYNC251 - wedging the loop is the point

    with Koil(cancel_timeout=0.2, shutdown_join_timeout=0.1):
        unkoil_task(block_the_loop)
        time.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(KoilTimeoutError):
            unkoil_with_timeout(asyncio.sleep, 0.1, 0)
        assert time.monotonic() - started < 1.2


# -- processes ------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:.*fork.*:DeprecationWarning")
def test_a_forked_child_gets_an_error_and_can_start_its_own_koil() -> None:
    """The loop thread does not survive a fork; waiting on it would never end."""

    async def answer() -> int:
        return 7

    with Koil():
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            code = 1
            try:
                try:
                    unkoil(answer)
                except KoilError:
                    with Koil():
                        if unkoil(answer) == 7:
                            code = 0
            finally:
                os._exit(code)

        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("the forked child hung")
        assert os.waitstatus_to_exitcode(status) == 0
        assert unkoil(answer) == 7  # the parent is untouched


# -- what a call hands back ---------------------------------------------------


def test_a_detached_task_does_not_undo_what_the_caller_changed_meanwhile() -> None:
    mine: contextvars.ContextVar[int] = contextvars.ContextVar("mine", default=0)
    theirs: contextvars.ContextVar[int] = contextvars.ContextVar("theirs", default=0)

    async def touch_theirs() -> None:
        await asyncio.sleep(0.05)
        theirs.set(1)

    def body() -> None:
        mine.set(1)
        future = unkoil_task(touch_theirs)
        mine.set(2)  # while the task runs
        future.result()
        assert mine.get() == 2
        assert theirs.get() == 1

    with Koil():
        contextvars.copy_context().run(body)


def test_an_async_koil_inside_a_call_leaves_the_callers_koil_alone() -> None:
    async def inner() -> int:
        async with Koil():
            return 1

    with Koil() as outer:
        loop = global_koil_loop.get()
        assert unkoil(inner) == 1
        assert global_koil.get() is outer
        assert global_koil_loop.get() is loop
        assert unkoil(inner) == 1


def test_koils_own_bookkeeping_is_never_handed_back() -> None:
    async def meddle() -> None:
        current_cancel_event.set(koil.context.KoilThreadSafeEvent())
        global_koil_loop.set(None)

    with Koil():
        loop = global_koil_loop.get()
        unkoil(meddle)
        assert current_cancel_event.get() is None
        assert global_koil_loop.get() is loop


async def test_an_async_koil_sets_and_clears_the_ambient_context() -> None:
    assert global_koil.get() is None
    async with Koil() as entered:
        assert global_koil.get() is entered
        assert global_koil_loop.get() is asyncio.get_running_loop()
        assert entered.running
    assert global_koil.get() is None
    assert global_koil_loop.get() is None
    assert not entered.running
