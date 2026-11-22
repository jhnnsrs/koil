import asyncio
import threading
from collections.abc import AsyncGenerator

import pytest

from koil.bridge import get_koiled_loop_or_raise, unkoil, unkoil_gen, unkoil_task
from koil.context import check_cancelled, current_cancel_event, global_koil_loop
from koil.errors import ContextError, KoilError, ThreadCancelledError
from koil.loop import Koil


async def _noop() -> int:
    return 1


async def _noop_gen() -> AsyncGenerator[int, None]:
    yield 1


def test_unkoil_without_koil_context() -> None:
    with pytest.raises(KoilError, match="No koil context found"):
        unkoil(_noop)


def test_unkoil_gen_without_koil_context() -> None:
    with pytest.raises(KoilError, match="No koil context found"):
        gen = unkoil_gen(_noop_gen)
        next(gen)


async def test_unkoil_from_async_code_says_await_it() -> None:
    # The sync wrapper reached for from an `async def`: there is a loop, but no
    # koil one, and the error has to name the fix rather than just "no context".
    with pytest.raises(KoilError, match="No koil context found") as info:
        unkoil(_noop)
    assert "running event loop" in str(info.value)
    assert "await _noop(...)" in str(info.value)


async def test_unkoil_gen_from_async_code_says_async_for_it() -> None:
    with pytest.raises(KoilError, match="No koil context found") as info:
        next(unkoil_gen(_noop_gen))
    assert "running event loop" in str(info.value)
    assert "async for ... in _noop_gen(...)" in str(info.value)


async def test_bound_method_from_async_code_names_the_async_method() -> None:
    class Task:
        async def aprogress(self) -> None: ...

        def progress(self) -> None:
            unkoil(self.aprogress)

    with pytest.raises(KoilError, match=r"await Task\.aprogress\(\.\.\.\)"):
        Task().progress()


def test_unkoil_without_any_loop_says_enter_koil() -> None:
    with pytest.raises(KoilError, match="with Koil\\(\\)"):
        unkoil(_noop)


async def test_koil_sync_in_async_false_raises() -> None:
    # Inside an async test there is a running event loop, so Koil(sync_in_async=False)
    # should reject __enter__ with ContextError.
    with pytest.raises(ContextError):
        with Koil(sync_in_async=False):
            pass


def test_closed_loop_raises() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    token = global_koil_loop.set(loop)
    try:
        with pytest.raises(RuntimeError, match="Loop is not running"):
            get_koiled_loop_or_raise()
    finally:
        global_koil_loop.reset(token)


def test_check_cancelled_raises_when_event_is_set() -> None:
    event = threading.Event()
    event.set()
    token = current_cancel_event.set(event)
    try:
        with pytest.raises(ThreadCancelledError):
            check_cancelled()
    finally:
        current_cancel_event.reset(token)


def test_check_cancelled_does_not_raise_when_event_is_clear() -> None:
    event = threading.Event()
    token = current_cancel_event.set(event)
    try:
        check_cancelled()  # should not raise
    finally:
        current_cancel_event.reset(token)


def test_unkoil_rejects_coroutine_object() -> None:
    """unkoil(fn()) — a natural asyncio.run-style mistake — gets a pointed error."""
    with pytest.raises(TypeError, match="coroutine object"):
        unkoil(_noop())


def test_unkoil_task_rejects_coroutine_object() -> None:
    with pytest.raises(TypeError, match="coroutine object"):
        unkoil_task(_noop())


def test_unkoil_gen_rejects_asyncgen_object() -> None:
    with pytest.raises(TypeError, match="async generator object"):
        gen = unkoil_gen(_noop_gen())
        next(gen)


def test_unkoil_coroutine_object_rejection_emits_no_unawaited_warning(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """The rejected coroutine is closed, so no 'never awaited' RuntimeWarning."""
    with pytest.raises(TypeError):
        unkoil(_noop())
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]
