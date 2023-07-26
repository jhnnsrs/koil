import contextvars

from koil.errors import ThreadCancelledError


current_loop = contextvars.ContextVar("current_loop", default=None)
current_cancel_event = contextvars.ContextVar("current_cancel_event", default=None)


output_queue_context = contextvars.ContextVar("out_queue_context")
input_queue_context = contextvars.ContextVar("input_queue_context")
in_process_context = contextvars.ContextVar("in_process_context", default=False)


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
