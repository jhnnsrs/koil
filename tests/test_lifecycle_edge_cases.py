"""Entering and leaving: ``@koilable`` objects, ``KoiledModel``, ``Composition``
and ``Koil`` itself, at the edges (failures on the way in or out, nesting,
copies) rather than on the happy path.
"""

import asyncio
import contextvars
import logging
import sys
import warnings
import threading
import time
from typing import Any

import pytest

from koil import Koil, KoilError, koilable, koiled_cm, run_threaded, unkoil
from koil.composition import Composition, KoiledModel
from koil.context import KoilThreadSafeEvent, global_koil, global_koil_loop


def loop_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "KoilLoop" and t.is_alive()}


async def one() -> int:
    return 1


# -- @koilable ------------------------------------------------------------------


@koilable
class Resource:
    def __init__(self, suppress: bool = False, fail_exit: bool = False) -> None:
        self.suppress = suppress
        self.fail_exit = fail_exit
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "Resource":
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.exited += 1
        if self.fail_exit:
            raise RuntimeError("exit failed")
        return self.suppress


def test_with_suppresses_an_exception_like_async_with_does() -> None:
    with Resource(suppress=True):
        raise ValueError("swallowed by __aexit__")

    with pytest.raises(ValueError):
        with Resource(suppress=False):
            raise ValueError("not swallowed")


def test_a_failing_exit_still_stops_the_loop_it_started() -> None:
    before = loop_threads()
    with pytest.raises(RuntimeError, match="exit failed"):
        with Resource(fail_exit=True):
            assert loop_threads() - before
    assert loop_threads() == before
    assert global_koil.get() is None
    assert global_koil_loop.get() is None


def test_entering_an_object_again_inside_its_own_block() -> None:
    resource = Resource()
    with resource:
        with resource:
            pass
        # The inner exit must not have stopped the loop the outer block runs on.
        assert unkoil(one) == 1
    assert (resource.entered, resource.exited) == (2, 2)
    assert global_koil.get() is None


def test_an_object_can_be_entered_again_after_it_was_left() -> None:
    resource = Resource()
    for _ in range(3):
        with resource:
            assert unkoil(one) == 1
    assert (resource.entered, resource.exited) == (3, 3)


def test_inside_a_koil_the_object_uses_that_loop() -> None:
    with Koil() as outer:
        loop = global_koil_loop.get()
        with Resource():
            assert global_koil.get() is outer
            assert global_koil_loop.get() is loop
        assert unkoil(one) == 1  # leaving the object left the Koil running


# -- KoiledModel ----------------------------------------------------------------


class Client(KoiledModel):
    name: str = "client"

    async def aget(self) -> str:
        return self.name

    def get(self) -> str:
        return unkoil(self.aget)


def test_enter_and_exit_without_a_with_block() -> None:
    client = Client()
    client.enter()
    try:
        assert client.get() == "client"
    finally:
        client.exit()
    assert global_koil.get() is None


def test_a_koil_set_on_the_object_is_the_one_it_runs_on() -> None:
    """What ``arkitekt.interactive`` does to let a notebook call sync methods."""
    client = Client()
    own = Koil(sync_in_async=True)
    setattr(client, "__koil", own)
    client.enter()
    try:
        assert global_koil.get() is own
        assert client.get() == "client"
    finally:
        client.exit()
    assert not own.running


async def test_sync_methods_work_under_a_running_loop_when_allowed() -> None:
    """A notebook cell: there is a running loop, and the call is still sync."""
    client = Client()
    setattr(client, "__koil", Koil(sync_in_async=True))
    client.enter()
    try:
        assert client.get() == "client"
    finally:
        client.exit()


def test_a_copy_does_not_share_the_originals_koil() -> None:
    client = Client()
    with client:
        shallow = client.model_copy()
        deep = client.model_copy(deep=True)
        assert "__koil" not in shallow.__dict__
        assert "__koil" not in deep.__dict__
        assert client.get() == "client"  # the original is undisturbed
    with shallow:
        assert shallow.get() == "client"
    assert shallow == client


# -- Composition ----------------------------------------------------------------


class Log:
    def __init__(self) -> None:
        self.events: list[str] = []


class Member(KoiledModel):
    name: str
    log: Any
    fail_enter: bool = False
    fail_exit: bool = False

    async def __aenter__(self) -> "Member":
        if self.fail_enter:
            raise RuntimeError(f"{self.name} cannot enter")
        self.log.events.append(f"enter {self.name}")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.log.events.append(f"exit {self.name}")
        if self.fail_exit:
            raise RuntimeError(f"{self.name} cannot exit")


class Trio(Composition):
    a: Member
    b: Member
    c: Member


def trio(log: Log, **overrides: dict[str, bool]) -> Trio:
    return Trio(
        **{
            name: Member(name=name, log=log, **overrides.get(name, {}))
            for name in ("a", "b", "c")
        }
    )


def test_members_are_exited_in_reverse_order() -> None:
    log = Log()
    with trio(log):
        pass
    assert log.events == ["enter a", "enter b", "enter c", "exit c", "exit b", "exit a"]


def test_a_member_failing_to_enter_unwinds_the_ones_before_it() -> None:
    log = Log()
    before = loop_threads()
    with pytest.raises(RuntimeError, match="b cannot enter"):
        with trio(log, b={"fail_enter": True}):
            pytest.fail("the block must not run")
    assert log.events == ["enter a", "exit a"]
    assert loop_threads() == before


def test_a_member_failing_to_exit_does_not_strand_the_others() -> None:
    log = Log()
    with pytest.raises(RuntimeError, match="b cannot exit"):
        with trio(log, b={"fail_exit": True}):
            pass
    assert log.events[3:] == ["exit c", "exit b", "exit a"]


async def test_a_composition_works_with_async_with() -> None:
    log = Log()
    async with trio(log):
        assert log.events == ["enter a", "enter b", "enter c"]
    assert log.events[3:] == ["exit c", "exit b", "exit a"]


def test_a_composition_can_be_entered_twice_in_a_row() -> None:
    log = Log()
    composition = trio(log)
    with composition:
        pass
    with composition:
        pass
    assert log.events.count("enter a") == 2 and log.events.count("exit a") == 2


class Owner(Composition):
    """Keeps a member it does not own as a field (a client holding its token loader)."""

    owned: Member
    borrowed: Member

    async def __aenter__(self) -> "Owner":
        await self.owned.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.owned.__aexit__(*exc)


def test_a_subclass_entering_only_what_it_owns() -> None:
    log = Log()
    owner = Owner(owned=Member(name="owned", log=log), borrowed=Member(name="borrowed", log=log))
    with owner:
        pass
    assert log.events == ["enter owned", "exit owned"]


# -- Koil -----------------------------------------------------------------------


def test_exiting_twice_is_harmless() -> None:
    koil = Koil()
    koil.enter()
    koil.exit()
    koil.exit()
    assert not koil.running


def test_a_nested_koil_shares_the_outer_loop_and_does_not_stop_it() -> None:
    with Koil() as outer:
        loop = global_koil_loop.get()
        with Koil():
            assert global_koil.get() is outer
            assert global_koil_loop.get() is loop
        assert unkoil(one) == 1


def test_one_slow_worker_does_not_look_like_a_wedged_loop(caplog) -> None:
    """A worker gets ``cancel_timeout`` to stop; the exit waits a bit longer than that."""
    inside = threading.Event()

    def slow() -> None:
        inside.set()
        time.sleep(0.6)

    async def main() -> None:
        asyncio.ensure_future(run_threaded(slow))
        await asyncio.get_running_loop().run_in_executor(None, inside.wait)

    with caplog.at_level(logging.WARNING, logger="koil.loop"):
        with Koil(cancel_timeout=0.3):
            unkoil(main)
    assert not [r for r in caplog.records if "did not stop" in r.getMessage()]


def test_tasks_left_on_the_loop_are_cancelled_on_exit() -> None:
    unwound = threading.Event()

    async def background() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            unwound.set()

    async def spawn() -> None:
        asyncio.ensure_future(background())
        await asyncio.sleep(0.01)

    with Koil():
        unkoil(spawn)
    assert unwound.is_set()


# -- the cancel event -------------------------------------------------------------


def test_the_event_is_set_at_once_and_runs_its_callbacks_once() -> None:
    event = KoilThreadSafeEvent()
    calls: list[str] = []
    event.add_callback(lambda: calls.append("first"))
    removed = event.add_callback(lambda: calls.append("removed"))
    removed()
    assert not event.is_set()
    event.set()
    event.set()
    assert event.is_set()
    assert calls == ["first"]
    # Registering on a set event runs right away.
    event.add_callback(lambda: calls.append("late"))
    assert calls == ["first", "late"]


def test_a_failing_callback_does_not_keep_the_others_from_running() -> None:
    event = KoilThreadSafeEvent()
    calls: list[str] = []

    def broken() -> None:
        raise RuntimeError("callback failed")

    event.add_callback(broken)
    event.add_callback(lambda: calls.append("ran"))
    event.set()
    assert calls == ["ran"]


def test_a_thread_can_block_on_the_event() -> None:
    event = KoilThreadSafeEvent()
    assert event.wait_blocking(0.01) is False
    threading.Timer(0.05, event.set).start()
    started = time.monotonic()
    assert event.wait_blocking(5) is True
    assert time.monotonic() - started < 2
    event.clear()
    assert not event.is_set()


async def test_the_event_can_be_awaited_and_set_from_another_thread() -> None:
    event = KoilThreadSafeEvent(asyncio.get_running_loop())
    threading.Timer(0.05, event.set).start()
    assert await asyncio.wait_for(event.wait(), timeout=5) is True
    assert await event.wait() is True  # already set: returns at once


async def test_a_cancelled_waiter_unregisters_itself() -> None:
    event = KoilThreadSafeEvent()
    waiter = asyncio.ensure_future(event.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not event._callbacks


# -- threads started inside a koil context ----------------------------------------


def in_thread(target: Any) -> list[Any]:
    """Run *target* in a plain thread; what it returned, raised, and warned."""
    outcome: list[Any] = []

    def body() -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                outcome.append(target())
            except BaseException as e:
                outcome.append(e)
            outcome.append([w for w in caught if w.category is RuntimeWarning])

    # An empty context is what a thread starts with by default; free-threaded
    # builds inherit the starter's instead, so ask for it where that is possible.
    fresh: dict[str, Any] = (
        {"context": contextvars.Context()} if sys.version_info >= (3, 14) else {}
    )
    thread = threading.Thread(target=body, name="plain-thread", **fresh)
    thread.start()
    thread.join(10)
    return outcome


def test_a_client_entered_in_a_plain_thread_warns_about_the_lost_context() -> None:
    def enter_client() -> str:
        with Client() as client:
            return client.get()

    with Koil():
        result, caught = in_thread(enter_client)

    assert result == "client"  # it still works, on a loop of its own
    assert len(caught) == 1
    message = str(caught[0].message)
    assert "plain-thread" in message
    assert "contextvars.copy_context().run" in message
    assert "run_threaded" in message


def test_koiled_cm_in_a_plain_thread_warns_too() -> None:
    class Plain:
        async def __aenter__(self) -> int:
            return 1

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def enter() -> int:
        with koiled_cm(Plain()) as value:
            return value

    with Koil():
        result, caught = in_thread(enter)
    assert result == 1 and len(caught) == 1


def test_unkoil_in_a_plain_thread_says_how_to_start_the_thread() -> None:
    with Koil():
        error, caught = in_thread(lambda: unkoil(one))

    assert isinstance(error, KoilError)
    assert "plain-thread" in str(error)
    assert "contextvars.copy_context().run" in str(error)
    assert not caught


def test_a_thread_started_with_the_context_is_fine() -> None:
    def enter_client() -> tuple[str, bool]:
        with Client() as client:
            return client.get(), global_koil.get() is outer

    outcome: list[Any] = []

    def body() -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            outcome.append(enter_client())
            outcome.append(list(caught))

    with Koil() as outer:
        thread = threading.Thread(target=contextvars.copy_context().run, args=(body,))
        thread.start()
        thread.join(10)

    assert outcome == [("client", True), []]


def test_an_explicit_koil_in_a_thread_is_a_decision_and_does_not_warn() -> None:
    def own_loop() -> int:
        with Koil():
            with Client() as client:
                return len(client.get())

    with Koil():
        result, caught = in_thread(own_loop)
    assert result == 6 and not caught


def test_without_any_koil_running_nothing_warns() -> None:
    def enter_client() -> str:
        with Client() as client:
            return client.get()

    result, caught = in_thread(enter_client)
    assert result == "client" and not caught
