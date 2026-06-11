"""Tests for the @koiled function decorator and the koiled_cm helper."""

import asyncio
from contextvars import ContextVar

import pytest

from koil import (
    Koil,
    KoilError,
    KoilTimeoutError,
    koiled,
    koiled_cm,
)
from koil.context import global_koil


@koiled
async def double(x: int) -> int:
    await asyncio.sleep(0)
    return x * 2


@koiled
async def counting(n: int):
    for i in range(n):
        await asyncio.sleep(0)
        yield i


# ---------------------------------------------------------------------------
# @koiled — functions
# ---------------------------------------------------------------------------


def test_koiled_sync_call_inside_koil():
    with Koil():
        assert double(21) == 42


async def test_koiled_async_call_returns_awaitable():
    assert await double(21) == 42


async def test_koiled_aio_attribute_is_the_original():
    assert await double.aio(21) == 42
    assert asyncio.iscoroutinefunction(double.aio)


def test_koiled_sync_call_without_context_raises():
    with pytest.raises(KoilError, match="No koil context found"):
        double(21)


def test_koiled_generator_sync():
    with Koil():
        assert list(counting(4)) == [0, 1, 2, 3]


async def test_koiled_generator_async():
    assert [i async for i in counting(4)] == [0, 1, 2, 3]


def test_koiled_with_timeout_expires():
    @koiled(timeout=0.05)
    async def slow():
        await asyncio.sleep(5)

    with Koil():
        with pytest.raises(KoilTimeoutError):
            slow()


def test_koiled_with_timeout_fast_returns():
    @koiled(timeout=5.0)
    async def fast(x: int) -> int:
        await asyncio.sleep(0)
        return x

    with Koil():
        assert fast(3) == 3


def test_koiled_timeout_does_not_collide_with_user_kwarg():
    @koiled(timeout=5.0)
    async def fn_with_timeout(timeout: float) -> float:
        await asyncio.sleep(0)
        return timeout

    with Koil():
        assert fn_with_timeout(timeout=1.25) == 1.25


def test_koiled_rejects_plain_function():
    with pytest.raises(TypeError, match="async def"):

        @koiled
        def not_async():
            pass


def test_koiled_rejects_decorating_classmethod_object():
    with pytest.raises(TypeError, match="above @koiled"):

        class C:
            @koiled
            @classmethod
            async def method(cls):
                pass


def test_koiled_instance_method_binding():
    class Service:
        def __init__(self, base: int):
            self.base = base

        @koiled
        async def offset(self, x: int) -> int:
            await asyncio.sleep(0)
            return self.base + x

    with Koil():
        assert Service(10).offset(5) == 15


def test_koiled_classmethod_above_koiled_works():
    class Service:
        base = 7

        @classmethod
        @koiled
        async def offset(cls, x: int) -> int:
            await asyncio.sleep(0)
            return cls.base + x

    with Koil():
        assert Service.offset(3) == 10


def test_koiled_negative_timeout_raises_at_decoration():
    with pytest.raises(ValueError):

        @koiled(timeout=-1)
        async def fn():
            pass


def test_koiled_contextvar_propagation():
    request_id: ContextVar[str] = ContextVar("request_id")

    @koiled
    async def read_var() -> str:
        return request_id.get()

    with Koil():
        token = request_id.set("req-9")
        try:
            assert read_var() == "req-9"
        finally:
            request_id.reset(token)


# ---------------------------------------------------------------------------
# koiled_cm
# ---------------------------------------------------------------------------


class AsyncResource:
    def __init__(self, fail_on_enter: bool = False, suppress: bool = False):
        self.entered = False
        self.exited = False
        self.fail_on_enter = fail_on_enter
        self.suppress = suppress

    async def __aenter__(self):
        if self.fail_on_enter:
            raise ValueError("enter failed")
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.exited = True
        return self.suppress


def test_koiled_cm_standalone_starts_and_tears_down_koil():
    resource = AsyncResource()
    with koiled_cm(resource) as r:
        assert r is resource
        assert r.entered
        assert global_koil.get() is not None
    assert resource.exited
    assert global_koil.get() is None


def test_koiled_cm_reuses_ambient_koil():
    resource = AsyncResource()
    with Koil() as outer:
        with koiled_cm(resource) as r:
            assert r.entered
            assert global_koil.get() is outer
        assert resource.exited
        # The ambient koil survives the inner block
        assert global_koil.get() is outer


def test_koiled_cm_enter_failure_tears_down_koil():
    resource = AsyncResource(fail_on_enter=True)
    with pytest.raises(ValueError, match="enter failed"):
        with koiled_cm(resource):
            pass
    assert global_koil.get() is None


def test_koiled_cm_exception_suppression_propagates():
    resource = AsyncResource(suppress=True)
    with koiled_cm(resource):
        raise RuntimeError("suppressed by __aexit__")
    assert resource.exited
    assert global_koil.get() is None


def test_koiled_cm_explicit_koil_is_used():
    resource = AsyncResource()
    my_koil = Koil(cancel_timeout=0.5)
    with koiled_cm(resource, koil=my_koil):
        assert global_koil.get() is my_koil
    assert my_koil.running is False
    assert global_koil.get() is None
