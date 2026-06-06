"""Tests for KoilThreadSafeEvent and unkoil_gen send-value semantics."""
import asyncio
import threading

import pytest

from koil.helpers import KoilThreadSafeEvent, unkoil_gen
from koil.koil import Koil


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
