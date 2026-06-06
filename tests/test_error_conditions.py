import asyncio
import threading
import pytest

from koil.errors import ContextError, KoilError, ThreadCancelledError
from koil.helpers import get_koiled_loop_or_raise, unkoil, unkoil_gen
from koil.koil import Koil
from koil.vars import check_cancelled, current_cancel_event, global_koil_loop


async def _noop() -> int:
    return 1


async def _noop_gen():
    yield 1


def test_unkoil_without_koil_context():
    with pytest.raises(KoilError, match="No koil context found"):
        unkoil(_noop)


def test_unkoil_gen_without_koil_context():
    with pytest.raises(KoilError, match="No koil context found"):
        gen = unkoil_gen(_noop_gen)
        next(gen)


async def test_koil_sync_in_async_false_raises():
    # Inside an async test there is a running event loop, so Koil(sync_in_async=False)
    # should reject __enter__ with ContextError.
    with pytest.raises(ContextError):
        with Koil(sync_in_async=False):
            pass


def test_closed_loop_raises():
    loop = asyncio.new_event_loop()
    loop.close()
    token = global_koil_loop.set(loop)
    try:
        with pytest.raises(RuntimeError, match="Loop is not running"):
            get_koiled_loop_or_raise()
    finally:
        global_koil_loop.reset(token)


def test_check_cancelled_raises_when_event_is_set():
    event = threading.Event()
    event.set()
    token = current_cancel_event.set(event)
    try:
        with pytest.raises(ThreadCancelledError):
            check_cancelled()
    finally:
        current_cancel_event.reset(token)


def test_check_cancelled_does_not_raise_when_event_is_clear():
    event = threading.Event()
    token = current_cancel_event.set(event)
    try:
        check_cancelled()  # should not raise
    finally:
        current_cancel_event.reset(token)
