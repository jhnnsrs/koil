import contextvars
from koil.errors import ThreadCancelledError
from typing import Optional, Protocol
import asyncio


class KoiledLoop(Protocol):
    @property
    def loop(self) -> asyncio.AbstractEventLoop: ...

    @property
    def cancel_timeout(self) -> float: ...

    @property
    def sync_in_async(self) -> bool: ...


class KoilThreadSafeEvent(asyncio.Event):
    """An ``asyncio.Event`` that can be set/cleared from any thread.

    The cancel signal in koil originates from arbitrary threads (a sync caller
    invoking ``KoilFuture.cancel()``, or the koil loop cancelling a spawned
    task) but must be awaited *inside* the koil loop. ``asyncio.Event`` is not
    thread-safe to mutate from outside its loop, so ``set``/``clear`` are routed
    through ``call_soon_threadsafe``. ``is_set()`` (a plain bool read) is used
    unchanged by cooperative sync consumers like ``check_cancelled``.

    ``_loop`` is bound at construction so set/clear target the koil loop even
    when called from a thread with no running loop of its own.
    """

    def __init__(self, koil_loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._loop = koil_loop

    def set(self):
        self._loop.call_soon_threadsafe(super().set)

    def clear(self):
        self._loop.call_soon_threadsafe(super().clear)



global_koil: contextvars.ContextVar[Optional[KoiledLoop]] = contextvars.ContextVar(
    "GLOBAL_KOIL", default=None
)
global_koil_loop: contextvars.ContextVar[Optional[asyncio.AbstractEventLoop]] = (
    contextvars.ContextVar("GLOBAL_KOIL_LOOP", default=None)
)


current_koil = contextvars.ContextVar("current_koil", default=None)

current_cancel_event: contextvars.ContextVar[Optional[asyncio.Event]] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)


def check_cancelled():
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        raise ThreadCancelledError("Task was cancelled")
