import contextvars

from koil.errors import ThreadCancelledError


current_loop = contextvars.ContextVar("current_loop", default=None)
current_cancel_event = contextvars.ContextVar("current_cancel_event", default=None)


def check_cancelled():
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        raise ThreadCancelledError("Task was cancelled")
