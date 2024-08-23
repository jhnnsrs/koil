import contextvars
import multiprocessing
from koil.errors import ThreadCancelledError


current_loop = contextvars.ContextVar("current_loop", default=None)


# Will only be set in the worker tread
current_cancel_event = contextvars.ContextVar("current_cancel_event", default=None)


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
