import contextvars
import multiprocessing
import threading
from koil.errors import ThreadCancelledError
from typing import Optional

current_loop = contextvars.ContextVar("current_loop", default=None)


current_koil = contextvars.ContextVar("current_koil", default=None)


# Will only be set in the worker tread
current_cancel_event: contextvars.ContextVar[Optional[threading.Event]] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)


# Will only be set in the worker process
output_queue_context: contextvars.ContextVar[multiprocessing.Queue] = (
    contextvars.ContextVar("out_queue_context")
)
input_queue_context: contextvars.ContextVar[multiprocessing.Queue] = (
    contextvars.ContextVar("input_queue_context")
)
in_process_context: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_process_context", default=False
)


def check_cancelled():
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        raise ThreadCancelledError("Task was cancelled")


def get_process_queues():
    """Get the process queues.

    Returns the input and output queues of the current process.

    Returns:
        multiprocessing.Queue: The input queue
        multiprocessing.Queue: The output queue
    """
    return input_queue_context.get(), output_queue_context.get()
