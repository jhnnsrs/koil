"""Tests that contextvars are correctly propagated into spawned threads."""
import contextvars

import pytest

from koil.composition.base import KoiledModel
from koil.bridge import (
    iterate_threaded,
    iterate_threaded_bridged,
    run_threaded,
    run_threaded_bridged,
)


request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="none"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SpawnModel(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read_var_in_thread(self) -> str:
        return await run_threaded(self._read)

    def _read(self) -> str:
        return request_id.get()

    async def set_var_in_thread(self, value: str) -> str:
        return await run_threaded(self._set, value)

    async def set_var_in_thread_bridged(self, value: str) -> str:
        return await run_threaded_bridged(self._set, value)

    def _set(self, value: str) -> str:
        request_id.set(value)
        return request_id.get()


class GenModel(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def yield_var_values(self, n: int):
        async for val in iterate_threaded(self._gen, n):
            yield val

    def _gen(self, n: int):
        for _ in range(n):
            yield request_id.get()

    async def yield_accumulated(self, n: int):
        async for val in iterate_threaded(self._accumulate_gen, n):
            yield val

    async def yield_accumulated_bridged(self, n: int):
        async for val in iterate_threaded_bridged(self._accumulate_gen, n):
            yield val

    def _accumulate_gen(self, n: int):
        # Each step reads whatever the *previous* step left in the ContextVar
        # (without re-deriving it locally), so the yielded values reveal whether
        # mutations persisted across yields.
        for i in range(n):
            prev = request_id.get()
            request_id.set(f"{i}" if prev == "none" else f"{prev}-{i}")
            yield request_id.get()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_context_var_propagates_into_run_spawned():
    """A ContextVar set before run_threaded is visible inside the spawned thread."""
    token = request_id.set("req-123")
    try:
        async with SpawnModel() as m:
            result = await m.read_var_in_thread()
        assert result == "req-123"
    finally:
        request_id.reset(token)


async def test_context_var_default_visible_in_run_spawned():
    """The default value of a ContextVar is visible inside the spawned thread."""
    async with SpawnModel() as m:
        result = await m.read_var_in_thread()
    assert result == "none"


async def test_context_var_propagates_into_iterate_spawned():
    """A ContextVar set before iterate_threaded is visible for every yielded value."""
    token = request_id.set("req-456")
    try:
        async with GenModel() as m:
            results = [v async for v in m.yield_var_values(3)]
        assert results == ["req-456", "req-456", "req-456"]
    finally:
        request_id.reset(token)


async def test_context_var_isolation_between_calls():
    """Different ContextVar values in consecutive calls don't bleed into each other."""
    async with SpawnModel() as m:
        t1 = request_id.set("first")
        r1 = await m.read_var_in_thread()
        request_id.reset(t1)

        t2 = request_id.set("second")
        r2 = await m.read_var_in_thread()
        request_id.reset(t2)

    assert r1 == "first"
    assert r2 == "second"


async def test_run_threaded_default_keeps_thread_isolation():
    """run_threaded (default): a var set inside the thread does not leak to the caller."""
    assert request_id.get() == "none"
    async with SpawnModel() as m:
        returned = await m.set_var_in_thread("set-in-thread")
    # The thread still sees its own set...
    assert returned == "set-in-thread"
    # ...but it does NOT leak back into the caller's context.
    assert request_id.get() == "none"


async def test_run_threaded_bridged_propagates_var_back_out():
    """run_threaded_bridged: a var set inside the thread is visible to the caller."""
    assert request_id.get() == "none"
    async with SpawnModel() as m:
        returned = await m.set_var_in_thread_bridged("set-in-thread")
    assert returned == "set-in-thread"
    # Bidirectional: the value set inside the worker thread is now visible here.
    assert request_id.get() == "set-in-thread"


async def test_iterate_threaded_default_isolates_each_step():
    """iterate_threaded (default): each step starts from the caller's value."""
    assert request_id.get() == "none"
    async with GenModel() as m:
        results = [v async for v in m.yield_accumulated(3)]
    # Every step re-reads the (unchanged) caller value "none", so no accumulation.
    assert results == ["0", "1", "2"]
    # And nothing leaked back into the caller.
    assert request_id.get() == "none"


async def test_iterate_threaded_bridged_propagates_var_back_out():
    """iterate_threaded_bridged: the generator's final var value is visible to the caller."""
    assert request_id.get() == "none"
    async with GenModel() as m:
        _ = [v async for v in m.yield_accumulated_bridged(3)]
    assert request_id.get() == "0-1-2"


async def test_iterate_threaded_bridged_persists_mutations_across_yields():
    """iterate_threaded_bridged: a var set in one step is observed by the next step."""
    async with GenModel() as m:
        results = [v async for v in m.yield_accumulated_bridged(3)]
    # Each step reads the previous step's value, so they accumulate.
    assert results == ["0", "0-1", "0-1-2"]
