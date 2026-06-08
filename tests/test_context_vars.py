"""Tests that contextvars are correctly propagated into spawned threads."""
import contextvars

import pytest

from koil.composition.base import KoiledModel
from koil.bridge import iterate_threaded, run_threaded


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
