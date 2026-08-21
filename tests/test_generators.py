"""Tests for KoilThreadSafeEvent and unkoil_gen send-value semantics."""
import asyncio
import threading

import pytest

from koil.bridge import KoilThreadSafeEvent, unkoil_gen
from koil.loop import Koil


# ---------------------------------------------------------------------------
# KoilThreadSafeEvent
# ---------------------------------------------------------------------------


async def test_thread_safe_event_set_from_background_thread():
    """KoilThreadSafeEvent.set() called from a non-event-loop thread wakes an async waiter."""
    loop = asyncio.get_running_loop()
    event = KoilThreadSafeEvent(loop)

    def setter():
        import time

        time.sleep(0.01)
        event.set()

    t = threading.Thread(target=setter, daemon=True)
    t.start()

    await asyncio.wait_for(event.wait(), timeout=2.0)
    assert event.is_set()
    t.join()


async def test_thread_safe_event_clear_from_background_thread():
    """KoilThreadSafeEvent.clear() called from a non-event-loop thread clears the event."""
    loop = asyncio.get_running_loop()
    event = KoilThreadSafeEvent(loop)

    # Use a regular asyncio.Event as a signal that the clear has been scheduled.
    cleared_signal = asyncio.Event()

    def clearer():
        import time

        time.sleep(0.01)
        event.clear()
        loop.call_soon_threadsafe(cleared_signal.set)

    # Start with the event set.
    await loop.run_in_executor(None, lambda: None)  # ensure loop is warm
    loop.call_soon_threadsafe(event._loop.call_soon_threadsafe, super(KoilThreadSafeEvent, event).set)  # type: ignore[misc]
    await asyncio.sleep(0.005)  # let the set propagate

    t = threading.Thread(target=clearer, daemon=True)
    t.start()

    await asyncio.wait_for(cleared_signal.wait(), timeout=2.0)
    assert not event.is_set()
    t.join()


# ---------------------------------------------------------------------------
# unkoil_gen: basic exhaustion and StopIteration
# ---------------------------------------------------------------------------


async def _simple_gen():
    yield 10
    yield 20
    yield 30


def test_unkoil_gen_exhausts_correctly():
    """unkoil_gen yields all values and then raises StopIteration."""
    with Koil():
        gen = unkoil_gen(_simple_gen)
        assert next(gen) == 10
        assert next(gen) == 20
        assert next(gen) == 30
        with pytest.raises(StopIteration):
            next(gen)


async def _single_value_gen():
    yield 99


def test_unkoil_gen_single_value():
    """unkoil_gen works for a generator that yields exactly one value."""
    with Koil():
        gen = unkoil_gen(_single_value_gen)
        assert next(gen) == 99
        with pytest.raises(StopIteration):
            next(gen)


# ---------------------------------------------------------------------------
# unkoil_gen: sending values into the async generator
# ---------------------------------------------------------------------------


async def _echo_gen():
    got = yield "first"
    while True:
        if got is None:
            got = yield "was-none"
        else:
            got = yield got * 2


def test_unkoil_gen_forwards_sent_values():
    """Non-None values sent into the sync generator reach the async generator
    via asend() (previously crashed with __anext__() taking no arguments)."""
    with Koil():
        gen = unkoil_gen(_echo_gen)
        assert next(gen) == "first"
        assert gen.send(21) == 42
        assert gen.send(5) == 10
        assert next(gen) == "was-none"
        gen.close()


def test_unkoil_gen_with_timeout_forwards_sent_values():
    from koil.bridge import unkoil_gen_with_timeout

    with Koil():
        gen = unkoil_gen_with_timeout(_echo_gen, 2.0)
        assert next(gen) == "first"
        assert gen.send(3) == 6
        gen.close()


def test_thread_safe_event_set_visible_even_when_loop_is_blocked():
    """set() must be visible to worker-side is_set() polls immediately, even
    while the loop cannot run callbacks (that is exactly when cooperative
    cancellation matters most)."""
    loop = asyncio.new_event_loop()  # never run: simulates a wedged loop
    try:
        event = KoilThreadSafeEvent(loop)
        assert not event.is_set()
        event.set()
        assert event.is_set()  # no loop iteration happened, still visible
        event.clear()
        assert not event.is_set()
    finally:
        loop.close()
