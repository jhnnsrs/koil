"""Re-entrancy and ambient-state hygiene of the Koil context manager."""

import asyncio
import threading

from koil.bridge import unkoil
from koil.context import global_koil, global_koil_loop
from koil.loop import Koil


async def _one() -> int:
    await asyncio.sleep(0)
    return 1


def _koil_loop_threads() -> "list[threading.Thread]":
    return [
        t
        for t in threading.enumerate()
        if t.name.startswith("KoiledLoop") and t.is_alive()
    ]


def test_same_instance_nested_reenter_keeps_loop_alive():
    """Nested `with` blocks on the SAME instance: the inner exit must not tear
    down the loop the outer block is still using."""
    koil = Koil()
    with koil:
        with koil:
            assert unkoil(_one) == 1
        # Previously the inner __exit__ stopped the loop here.
        assert unkoil(_one) == 1
    assert global_koil.get() is None
    assert global_koil_loop.get() is None


def test_nested_distinct_instances_restore_outer_ambient_state():
    """A nested Koil is a no-op and its exit restores (not clears) the outer
    instance's ambient contextvars."""
    outer = Koil()
    with outer:
        with Koil():
            assert unkoil(_one) == 1
        assert global_koil.get() is outer
        assert unkoil(_one) == 1
    assert global_koil.get() is None


def test_exit_without_enter_is_a_noop():
    koil = Koil()
    koil.__exit__(None, None, None)  # must not raise or corrupt state
    with koil:
        assert unkoil(_one) == 1


def test_loop_thread_stopped_after_exit():
    before = len(_koil_loop_threads())
    with Koil():
        assert len(_koil_loop_threads()) == before + 1
    assert len(_koil_loop_threads()) == before


async def test_async_enter_sets_and_exit_restores_both_ambient_vars():
    """`async with Koil()` sets both global_koil and global_koil_loop and its
    exit restores previous values (the old code set one and cleared the
    other)."""
    assert global_koil.get() is None
    koil = Koil()
    async with koil:
        assert global_koil.get() is koil
        assert global_koil_loop.get() is asyncio.get_running_loop()
    assert global_koil.get() is None
    assert global_koil_loop.get() is None


async def test_sync_exit_never_stops_async_captured_loop():
    """A sync __exit__ after `async with` must not stop the user's own loop,
    which the instance captured but does not own."""
    koil = Koil()
    async with koil:
        pass
    koil.__exit__(None, None, None)
    # If the captured loop had been stopped/closed this await would fail.
    await asyncio.sleep(0)
