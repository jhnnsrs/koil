import asyncio
import contextvars
import concurrent.futures
import threading
from typing import Any, Callable
from koil.errors import CancelledError


async def check_event(event: threading.Event, timeout: float = None):
    """
    Waits for an event to be set, or for a timeout to occur.
    """
    while True:
        await asyncio.sleep(0.00001)
        if event.is_set():
            return


def run_threaded_with_context(
    coro: Callable,
    loop: asyncio.AbstractEventLoop,
    cancel_event: threading.Event,
    *args: Any,
    **kwargs: Any,
) -> concurrent.futures.Future:
    """Runs a future in the supplied loop but copiesthe context
    of the current loop,

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future + newcontext
    """

    ctxs = contextvars.copy_context()

    async def passed_with_context(coro):
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            result = await coro(*args, **kwargs)

            newcontext = contextvars.copy_context()
            return result, newcontext

        cancel_f = asyncio.create_task(check_event(cancel_event))
        future_t = asyncio.create_task(context_future())

        finished, unfinished = await asyncio.wait(
            [future_t, cancel_f], return_when=asyncio.FIRST_COMPLETED
        )

        for task in finished:
            if task == cancel_f:
                for untask in unfinished:
                    untask.cancel()
                    try:
                        await untask
                    except asyncio.CancelledError:
                        pass  # we are not interested in this and it should always be fine

                raise CancelledError(f"Future {task} was cancelled")

            for untask in unfinished:
                untask.cancel()
                try:
                    await untask
                except asyncio.CancelledError:
                    pass  # we are not interested in this and it should always be fine

            if task.exception():
                raise task.exception()

            return task.result()

    return asyncio.run_coroutine_threadsafe(passed_with_context(coro), loop)


def run_threaded_with_context_and_signals(
    coro: Callable,
    loop: asyncio.AbstractEventLoop,
    cancel_event: threading.Event,
    returned_signal: Any,
    errored_signal: Any,
    cancelled_signal: Any,
    *args,
    **kwargs,
) -> concurrent.futures.Future:
    """Runs a future in the supplied loop but copiesthe context
    of the current loop,

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future + newcontext
    """

    ctxs = contextvars.copy_context()

    async def passed_with_context(coro):
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            result = await coro(*args, **kwargs)
            newcontext = contextvars.copy_context()
            return result, newcontext

        cancel_f = asyncio.create_task(check_event(cancel_event))
        future_t = asyncio.create_task(context_future())

        finished, unfinished = await asyncio.wait(
            [future_t, cancel_f], return_when=asyncio.FIRST_COMPLETED
        )
        for task in finished:
            if task == cancel_f:
                for untask in unfinished:
                    untask.cancel()
                    try:
                        await untask
                    except asyncio.CancelledError:
                        pass  # we are not interested in this and it should always be fine

                cancelled_signal.emit()
                raise CancelledError(f"Future {task} was cancelled")

            for untask in unfinished:
                untask.cancel()
                try:
                    await untask
                except asyncio.CancelledError:
                    pass  # we are not interested in this and it should always be fine

            if task.exception():
                errored_signal.emit(task.exception())
                raise task.exception()

            returned_signal.emit(*task.result())
            return task.result()

    return asyncio.run_coroutine_threadsafe(passed_with_context(coro), loop)
