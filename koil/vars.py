import contextvars
import threading
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


global_koil: contextvars.ContextVar[Optional[KoiledLoop]] = contextvars.ContextVar(
    "GLOBAL_KOIL", default=None
)
global_koil_loop: contextvars.ContextVar[Optional[asyncio.AbstractEventLoop]] = (
    contextvars.ContextVar("GLOBAL_KOIL_LOOP", default=None)
)


current_koil = contextvars.ContextVar("current_koil", default=None)

current_cancel_event: contextvars.ContextVar[Optional[threading.Event]] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)


def check_cancelled():
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        raise ThreadCancelledError("Task was cancelled")
