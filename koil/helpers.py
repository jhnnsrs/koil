import asyncio
from koil.vars import *
import time


def unkoil_gen(iterator, *args, timeout=None, **kwargs):
    loop = current_loop.get()
    cancel_event = current_cancel_event.get()

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

    async def next_on_ait():
        try:
            try:
                obj = await ait.__anext__()
                return [False, obj]
            except StopAsyncIteration:
                return [True, None]
        except asyncio.CancelledError as e:
            return [False, e]

    while True:
        res = asyncio.run_coroutine_threadsafe(next_on_ait(), loop=loop)
        while not res.done():
            if cancel_event and cancel_event.is_set():
                raise Exception("Task was cancelled")

            time.sleep(0.01)
        done, obj = res.result()
        if done:
            if obj:
                raise obj
            break
        yield obj


def unkoil(coro, *args, timeout=None, as_task=False, ensure_koiled=False, **kwargs):
    try:
        loop0 = asyncio.events.get_running_loop()
        if ensure_koiled:
            raise NotImplementedError(
                "Calling sync() from within a running loop, async that bitch"
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
                taskclass = current_task_class.get()
                assert taskclass is not None, "No task class set"
                return taskclass(passed_with_context(), loop).run()

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
                """No loop is running. That means you cannot have this run as a task. Try providing a loop by entering a Koil() context.\n

                If you spawned a thread, in an Excecutor. You can use check_cancel() if you want to check if the thread was cancelled. Do this periodically
                """
            )

        future = coro(*args, **kwargs)
        asyncio.run(future)
