import asyncio
import threading

import janus
from koil.errors import (
    KoilError,
    KoilStopIteration,
    ThreadCancelledError,
)
from koil.task import KoilFuture, KoilRunner
from koil.utils import run_threaded_with_context
from koil.vars import *
import time
import logging


def unkoil_gen(iterator, *args, **kwargs):

    loop = current_loop.get()
    try:
        loop0 = asyncio.events.get_running_loop()
        if not loop or loop0 == loop:
            return iterator(*args, **kwargs)
    except RuntimeError:
        pass

    assert loop, "No koiled loop found"

    cancel_event = current_cancel_event.get() or threading.Event()

    if loop.is_closed():
        raise RuntimeError("Loop is not running")
    try:
        loop0 = asyncio.events.get_running_loop()
        if loop0 is loop:
            raise NotImplementedError("Calling sync() from within a running loop")
    except RuntimeError:
        pass

    ait = iterator(*args, **kwargs).__aiter__()
    res = [False, False]
    next_args = None

    async def next_on_ait(inside_args):
        try:
            try:
                if inside_args:
                    obj = await ait.__anext__(*inside_args)
                else:
                    obj = await ait.__anext__()
                return [False, obj]
            except StopAsyncIteration:
                return [True, None]
        except asyncio.CancelledError as e:
            return [False, e]

    while True:
        res = run_threaded_with_context(next_on_ait, loop, cancel_event, next_args)
        x, context = res.result()
        done, obj = x
        if done:
            if obj:
                raise obj
            break

        for ctx, value in context.items():
            ctx.set(value)

        next_args = yield obj


def unkoil(coro, *args, **kwargs):
    loop = current_loop.get()
    try:
        loop0 = asyncio.events.get_running_loop()
        if not loop or loop0 == loop:
            return coro(
                *args, **kwargs
            )  # We are running in an event loop so we can just return the coroutine

    except RuntimeError:
        pass

    loop = current_loop.get()
    cancel_event = current_cancel_event.get()

    if loop:
        try:
            if loop.is_closed():
                raise RuntimeError("Loop is not running")

            ctxs = contextvars.copy_context()

            async def passed_with_context():
                for ctx, value in ctxs.items():
                    ctx.set(value)

                x = await coro(*args, **kwargs)
                newcontext = contextvars.copy_context()
                return x, newcontext

            co_future = asyncio.run_coroutine_threadsafe(passed_with_context(), loop)
            while not co_future.done():
                time.sleep(0.01)
                if cancel_event and cancel_event.is_set():
                    raise ThreadCancelledError("Task was cancelled")

            x, newcontext = co_future.result()

            for ctx, value in newcontext.items():
                ctx.set(value)

            return x

        except KeyboardInterrupt:
            logging.info("Grace period triggered?")
            raise

    raise NotImplementedError(
        f"You need to be in a Koil() context to use sync() {coro} {loop}"
    )


async def run_spawned(
    sync_func,
    *sync_args,
    executor=None,
    pass_context=False,
    pass_loop=True,
    cancel_timeout=None,
    **sync_kwargs,
):
    """
    Spawn a thread with a given sync function and arguments
    """

    loop = current_loop.get()
    try:
        loop0 = asyncio.get_event_loop()
        if loop:
            assert loop0 is loop, "Loop is not the same"
        else:
            loop = loop0
    except RuntimeError:
        loop = current_loop.get()

    assert loop, "No koiled loop found"
    assert loop.is_running(), "Loop is not running"

    def wrapper(sync_args, sync_kwargs, loop, cancel_event, context):
        if pass_loop:
            current_loop.set(loop)

        current_cancel_event.set(cancel_event)

        if context:
            for ctx, value in context.items():
                ctx.set(value)

        logging.debug("New thread spawned")
        if sync_args:
            return sync_func(*sync_args, **sync_kwargs)
        else:
            try:
                return sync_func(**sync_kwargs)
            except Exception as e:
                logging.info("Exception in thread", exc_info=True)
                raise e

    context = contextvars.copy_context() if pass_context else None
    cancel_event = threading.Event()

    f = loop.run_in_executor(
        executor,
        wrapper,
        sync_args if len(sync_args) > 0 else None,
        sync_kwargs,
        loop,
        cancel_event,
        context,
    )
    try:
        shielded_f = await asyncio.shield(f)
        return shielded_f
    except asyncio.CancelledError as e:
        cancel_event.set()

        try:
            await asyncio.wait_for(f, timeout=cancel_timeout)
        except ThreadCancelledError:
            logging.info("Future in another thread was sucessfully cancelled")
        except asyncio.TimeoutError as te:
            raise KoilError(
                f"We could not successfully cancel the future {f} another thread. Make sure you are not blocking the thread with a long running task and check if you check_cancelled every now and then"
            ) from te

        raise e


async def iterate_spawned(
    sync_gen,
    *sync_args,
    executor=None,
    pass_context=False,
    pass_loop=True,
    cancel_timeout=None,
    **sync_kwargs,
):
    """
    Spawn a thread with a given sync function and arguments
    """

    loop = current_loop.get()
    try:
        loop0 = asyncio.get_event_loop()
        if loop:
            assert loop0 is loop, "Loop is not the same"
        else:
            loop = loop0
    except RuntimeError:
        loop = current_loop.get()

    assert loop, "No koiled loop found"
    assert loop.is_running(), "Loop is not running"

    yield_queue = janus.Queue()
    next_queue = janus.Queue()
    cancel_event = threading.Event()

    def wrapper(
        sync_args,
        sync_kwargs,
        sync_yield_queue,
        sync_next_queue,
        loop,
        cancel_event,
        context,
    ):
        if pass_loop:
            current_loop.set(loop)

        current_cancel_event.set(cancel_event)

        if context:
            for ctx, value in context.items():
                ctx.set(value)

        it = sync_gen(*sync_args, **sync_kwargs).__iter__()

        args = ()
        while True:
            try:
                res = it.__next__(*args if args else ())
                if cancel_event.is_set():
                    raise ThreadCancelledError("Thread was cancelled")
                sync_yield_queue.put(res)
                args = sync_next_queue.get()
                sync_next_queue.task_done()
            except StopIteration as e:
                raise KoilStopIteration("Thread stopped")
            except Exception as e:
                logging.info("Exception in generator", exc_info=True)
                raise e

    context = contextvars.copy_context() if pass_context else None

    f = loop.run_in_executor(
        executor,
        wrapper,
        sync_args,
        sync_kwargs,
        yield_queue.sync_q,
        next_queue.sync_q,
        loop,
        cancel_event,
        context,
    )

    try:
        while True:

            it_task = asyncio.create_task(yield_queue.async_q.get())

            finish, unfinished = await asyncio.wait(
                [it_task, f], return_when=asyncio.FIRST_COMPLETED
            )

            finish_condition = False

            for task in finish:
                if task == f:
                    if task.exception():
                        finish_condition = task.exception()

                else:
                    yield_queue.async_q.task_done()
                    x = yield task.result()
                    await next_queue.async_q.put(x)

            if finish_condition:
                yield_queue.close()
                try:
                    raise finish_condition
                except KoilStopIteration:
                    break
    except asyncio.CancelledError as e:
        cancel_event.set()

        finish, unfinished = await asyncio.wait(
            [it_task, f], return_when=asyncio.FIRST_COMPLETED
        )

        raise e


def create_task(coro, *args, **kwargs) -> KoilFuture:
    return KoilRunner(coro, preset_args=args, preset_kwargs=kwargs).run()


def create_runner(coro, *args, **kwargs) -> KoilRunner:
    return KoilRunner(coro, preset_args=args, preset_kwargs=kwargs)
