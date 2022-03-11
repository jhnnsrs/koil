import asyncio
from asyncio.log import logger
import threading

import janus
from koil.errors import KoilError, KoilStopIteration, ThreadCancelledError
from koil.utils import run_threaded_with_context
from koil.vars import *
from koil.task import KoilTask
import time


def create_task(coro, *args, **kwargs) -> KoilTask:
    """Create a coil task from a coroutine"""
    return coro(*args, **kwargs, as_task=True)


def unkoil_gen(iterator, *args, timeout=None, **kwargs):
    loop = current_loop.get()
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
    next_args = ()

    async def next_on_ait(next_args):
        try:
            try:
                obj = await ait.__anext__(*next_args if next_args else ())
                return [False, obj]
            except StopAsyncIteration:
                return [True, None]
        except asyncio.CancelledError as e:
            return [False, e]

    while True:
        res = run_threaded_with_context(
            next_on_ait(next_args), loop, cancel_event=cancel_event
        )
        x, context = res.result()
        done, obj = x
        if done:
            if obj:
                raise obj
            break

        for ctx, value in context.items():
            ctx.set(value)

        next_args = yield obj


def unkoil(coro, *args, timeout=None, as_task=False, ensure_koiled=False, **kwargs):
    try:
        asyncio.events.get_running_loop()
        if ensure_koiled:
            raise NotImplementedError(
                "Calling sync() from within a running loop, you need to await the coroutine"
            )

        return coro(*args, **kwargs)
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

            if as_task:
                taskclass = current_taskclass.get()
                assert taskclass is not None, "No task class set"
                return taskclass(
                    coro, preset_args=args, preset_kwargs=kwargs, loop=loop
                )

            co_future = asyncio.run_coroutine_threadsafe(passed_with_context(), loop)
            while not co_future.done():
                time.sleep(0.01)
                if cancel_event and cancel_event.is_set():
                    raise Exception("Task was cancelled")

            x, newcontext = co_future.result()

            for ctx, value in newcontext.items():
                ctx.set(value)

            return x

        except KeyboardInterrupt:
            print("Grace period triggered?")
            raise
    else:
        if ensure_koiled:
            raise RuntimeError("No loop set and ensure_koiled was set to True")

        if as_task:
            raise RuntimeError(
                """No loop is running. That means you cannot have this run as a task. Try providing a loop by entering a Koil() context.
                """
            )

        logger.warn(
            "You used unkoil without a governing Koil in the context, this is not recommended. We will now resort to run asyncio.run()"
        )

        future = coro(*args, **kwargs)
        asyncio.run(future)


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
            current_taskclass.set(KoilTask)
    except RuntimeError:
        loop = current_loop.get()

    assert loop, "No koiled loop found"
    assert loop.is_running(), "Loop is not running"

    def wrapper(sync_args, sync_kwargs, loop, cancel_event, context):
        if pass_loop:
            current_loop.set(loop)
            current_taskclass.set(KoilTask)

        current_cancel_event.set(cancel_event)

        if context:
            for ctx, value in context.items():
                ctx.set(value)

        print("New thread spawned")
        return sync_func(*sync_args, **sync_kwargs)

    context = contextvars.copy_context() if pass_context else None
    cancel_event = threading.Event()

    f = loop.run_in_executor(
        None, wrapper, sync_args, sync_kwargs, loop, cancel_event, context
    )
    try:
        return await asyncio.shield(f)
    except asyncio.CancelledError as e:
        cancel_event.set()

        try:
            await asyncio.wait_for(f, timeout=cancel_timeout)
        except ThreadCancelledError:
            print("Thread was cancelled")
            logger.info("Future in another thread was sucessfully cancelled")
            pass
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
            current_taskclass.set(KoilTask)
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
            current_taskclass.set(KoilTask)

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
                print(e)
                raise e

    context = contextvars.copy_context() if pass_context else None

    f = loop.run_in_executor(
        None,
        wrapper,
        sync_args,
        sync_kwargs,
        yield_queue.sync_q,
        next_queue.sync_q,
        loop,
        cancel_event,
        context,
    )

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
