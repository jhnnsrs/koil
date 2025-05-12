import asyncio
from collections.abc import AsyncGenerator
import concurrent.futures
import threading

import concurrent
import janus
from koil.errors import (
    KoilError,
    KoilStopIteration,
    ThreadCancelledError,
)
from typing import AsyncGenerator
from koil.utils import run_threaded_with_context
from koil.vars import (
    current_loop,
    current_cancel_event,
)
from koil.koil import global_koil, global_koil_loop
import contextvars
import time
import logging
from typing import Callable, Dict, Generic, Tuple, TypeVar

try:
    from typing import ParamSpec
except ImportError:
    from typing_extensions import ParamSpec


from typing import Coroutine, Any, Union, Awaitable, Generator

P = ParamSpec("P")
T = TypeVar("T")
R = TypeVar("R")


def unkoil_gen(
    iterator: Callable[P, AsyncGenerator[R, None]], *args: P.args, **kwargs: P.kwargs
) -> Generator[R, None, None]:
    koil_loop = global_koil_loop.get()

    if not koil_loop:
        raise RuntimeError("No koil context found")

    try:
        loop0 = asyncio.events.get_running_loop()
        if koil_loop == loop0:
            raise NotImplementedError(
                "Calling unkoil() from within a running loop. This is not supported"
            )
        else:
            koil = global_koil.get()
            if koil:
                if not koil.sync_in_async:
                    raise NotImplementedError(
                        "Calling unkoil() from within a running loop. while koil doens't allow it. This is not supported"
                    )
            else:
                # We are neither in a top level koil loop (i.e. we have always been async)
                raise NotImplementedError(
                    "Calling unkoil() from within a running loop. while koil doens't allow it. This is not supported"
                )
            # TODO: Check if async in sync is set to true
            pass
    except RuntimeError:
        pass

    if koil_loop.is_closed():
        raise RuntimeError("Loop is not running")

    cancel_event = current_cancel_event.get() or threading.Event()

    ait = iterator(*args, **kwargs).__aiter__()
    res = [False, False]
    next_args = None

    async def next_on_ait(inside_args: Tuple[Any] | None = None) -> Tuple[bool, Any]:
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
        res = run_threaded_with_context(next_on_ait, koil_loop, cancel_event, next_args)
        x, context = res.result()
        done, obj = x
        if done:
            if obj:
                raise obj
            break

        for ctx, value in context.items():
            ctx.set(value)

        next_args = yield obj


def unkoil(
    coro: Union[
        Callable[P, Coroutine[Any, Any, R]],
        Callable[P, Awaitable[R]],
    ],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    koil_loop = global_koil_loop.get()

    if not koil_loop:
        raise RuntimeError("No koil context found")

    try:
        loop0 = asyncio.events.get_running_loop()
        if koil_loop == loop0:
            raise NotImplementedError(
                "Calling unkoil() from within a running loop. This is not supported"
            )
        else:
            koil = global_koil.get()
            if koil:
                if not koil.sync_in_async:
                    raise NotImplementedError(
                        "Calling unkoil() from within a running loop. while koil doens't allow it. This is not supported"
                    )
            else:
                # We are neither in a top level koil loop (i.e. we have always been async)
                raise NotImplementedError(
                    "Calling unkoil() from within a running loop. while koil doens't allow it. This is not supported"
                )
            # TODO: Check if async in sync is set to true
            pass
    except RuntimeError:
        pass

    if koil_loop.is_closed():
        raise RuntimeError("Loop is not running")

    cancel_event = current_cancel_event.get()

    try:
        ctxs = contextvars.copy_context()

        async def passed_with_context():
            for ctx, value in ctxs.items():
                ctx.set(value)

            x = await coro(*args, **kwargs)
            newcontext = contextvars.copy_context()
            return x, newcontext

        co_future = asyncio.run_coroutine_threadsafe(passed_with_context(), koil_loop)
        while not co_future.done():
            time.sleep(0.01)
            # This should go by using an asyncio.Event rather than a time.sleep
            # i.e we would await the cancel event and the future at the same time
            # in the other thread
            if cancel_event and cancel_event.is_set():
                co_future.cancel()
                raise ThreadCancelledError("Task was cancelled")

        x, newcontext = co_future.result()

        for ctx, value in newcontext.items():
            ctx.set(value)

        return x

    except KeyboardInterrupt:
        logging.info("Grace period triggered?")
        raise


class KoilThreadSafeEvent(asyncio.Event):
    def __init__(self, koil_loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._loop = koil_loop

    def set(self):
        self._loop.call_soon_threadsafe(super().set)

    def clear(self):
        self._loop.call_soon_threadsafe(super().clear)


TaskReturn = TypeVar("TaskReturn")
TaskArgs = TypeVar("TaskArgs")
TaskNext = TypeVar("TaskNext")


class KoilTask(Generic[TaskReturn]):
    def __init__(
        self,
        future: concurrent.futures.Future[Tuple[TaskReturn, contextvars.Context]],
        cancel_event: KoilThreadSafeEvent,
    ) -> None:
        self.future = future
        self.cancel_event = cancel_event

    def result(self) -> TaskReturn:
        assert self.future, "Task was never run! Please run task before"

        res, context = self.future.result()

        for ctx, value in context.items():
            ctx.set(value)

        return res

    def done(self):
        return self.future.done()

    def cancel(self):
        return self.cancel_event.set()


class KoilGeneratorTask(Generic[TaskReturn, TaskNext]):
    def __init__(
        self,
        future: concurrent.futures.Future[Tuple[TaskReturn, contextvars.Context]],
        cancel_event: KoilThreadSafeEvent,
    ) -> None:
        self.future = future
        self.cancel_event = cancel_event

    def next(
        self,
    ) -> TaskReturn:
        assert self.future, "Task was never run! Please run task before"

        res, context = self.future.result()

        for ctx, value in context.items():
            ctx.set(value)

        return res

    def done(self):
        return self.future.done()

    def cancel(self):
        return self.cancel_event.set()


def unkoil_task(
    coro: Union[
        Callable[P, Coroutine[Any, Any, R]],
        Callable[P, Awaitable[R]],
    ],
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilTask[R]:
    koil_loop = global_koil_loop.get()

    if not koil_loop:
        raise RuntimeError("No koil context found")

    try:
        loop0 = asyncio.events.get_running_loop()
        if koil_loop == loop0:
            raise NotImplementedError(
                "Calling unkoil() from within a running loop. This is not supported"
            )
        else:
            koil = global_koil.get()
            if koil:
                if not koil.sync_in_async:
                    raise NotImplementedError(
                        "Calling unkoil() from within a running loop. while koil doens't allow it. This is not supported"
                    )
            else:
                # We are neither in a top level koil loop (i.e. we have always been async)
                raise NotImplementedError(
                    "Calling unkoil() from within a running loop. while koil doens't allow it. This is not supported"
                )
            # TODO: Check if async in sync is set to true
            pass
    except RuntimeError:
        pass

    if koil_loop.is_closed():
        raise RuntimeError("Loop is not running")

    try:
        ctxs = contextvars.copy_context()

        cancel_event = KoilThreadSafeEvent(koil_loop)

        async def passed_with_context(cancel_event):
            async def await_cancelation(cancel_event: asyncio.Event):
                await cancel_event.wait()
                print("Cancel event triggered")

            await_cancelation_task = asyncio.create_task(
                await_cancelation(cancel_event)
            )
            task = asyncio.create_task(coro(*args, **kwargs))

            done, pending = await asyncio.wait(
                {await_cancelation_task, task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if await_cancelation_task in done:
                print("Cancel event triggered canceling normal tasK")
                task.cancel()

                try:
                    await task
                except asyncio.CancelledError:
                    pass

                raise ThreadCancelledError("Task was cancelled")

            else:
                await_cancelation_task.cancel()

                try:
                    await await_cancelation_task
                except asyncio.CancelledError:
                    pass

                x = await task
                return x, contextvars.copy_context()

        co_future = asyncio.run_coroutine_threadsafe(
            passed_with_context(cancel_event), koil_loop
        )
        print("Waiting for future")
        return KoilTask(co_future, cancel_event)

    except KeyboardInterrupt:
        logging.info("Grace period triggered?")
        raise


async def run_spawned(
    sync_func: Callable[P, R],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> R:
    """
    Spawn a thread with a given sync function and arguments
    """
    loop = asyncio.get_running_loop()

    koil = global_koil.get()
    if koil:
        koil_loop = koil.loop
        assert koil_loop == loop, (
            "You are trying to run a koil generator in a different loop than the one it was created in"
        )

    def wrapper(
        cancel_event: threading.Event,
        context: contextvars.Context,
        sync_args: Tuple[Any],
        sync_kwargs: Dict[str, Any],
    ) -> R:
        global_koil.set(koil)
        global_koil_loop.set(loop)
        current_cancel_event.set(cancel_event)

        if context:
            for ctx, value in context.items():
                ctx.set(value)

        logging.debug("New thread spawned")

        return sync_func(*sync_args, **sync_kwargs)

    context = contextvars.copy_context()
    cancel_event = threading.Event()

    future = loop.run_in_executor(
        None,
        wrapper,
        cancel_event,
        context,
        sync_args,
        sync_kwargs,
    )  # type: ignore
    try:
        shielded_f = await asyncio.shield(future)
        return shielded_f
    except asyncio.CancelledError as e:
        cancel_event.set()

        try:
            await asyncio.wait_for(future, timeout=koil.cancel_timeout if koil else 2)
        except ThreadCancelledError:
            logging.info("Future in another thread was sucessfully cancelled")
        except asyncio.TimeoutError as te:
            raise KoilError(
                f"We could not successfully cancel the future {future} another thread. Make sure you are not blocking the thread with a long running task and check if you check_cancelled every now and then"
            ) from te

        raise e


S = TypeVar("S")


async def iterate_spawned(
    sync_gen: Callable[P, Generator[R, S, None]],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> AsyncGenerator[R, S]:
    """
    Spawn a thread with a given sync function and arguments
    """

    loop = asyncio.get_running_loop()

    koil = global_koil.get()
    if koil:
        koil_loop = koil.loop
        assert koil_loop == loop, (
            "You are trying to run a koil generator in a different loop than the one it was created in"
        )

    yield_queue: janus.Queue[R] = janus.Queue()
    next_queue: janus.Queue[S] = janus.Queue()
    cancel_event = threading.Event()

    def wrapper(
        cancel_event: threading.Event,
        context: contextvars.Context,
        sync_yield_queue: janus.SyncQueue[R],
        sync_next_queue: janus.SyncQueue[S],
        sync_args: Tuple[Any],
        sync_kwargs: Dict[str, Any],
    ) -> None:
        global_koil_loop.set(loop)

        current_cancel_event.set(cancel_event)

        if context:
            for ctx, value in context.items():
                ctx.set(value)

        it = sync_gen(*sync_args, **sync_kwargs).__iter__()  # type: ignore

        args = ()
        while True:
            try:
                res = it.__next__(*args if args else ())  # type: ignore
                print("Yielding result", res)
                if cancel_event.is_set():
                    raise ThreadCancelledError("Thread was cancelled")
                sync_yield_queue.put(res)
                args = sync_next_queue.get()
                sync_next_queue.task_done()
            except StopIteration:
                raise KoilStopIteration("Thread stopped")
            except Exception as e:
                logging.info("Exception in generator", exc_info=True)
                raise e

    context = contextvars.copy_context()

    iterator_future = loop.run_in_executor(
        None,
        wrapper,
        cancel_event,
        context,
        yield_queue.sync_q,
        next_queue.sync_q,
        sync_args,
        sync_kwargs,
    )  # type: ignore

    try:
        while True:
            it_task = asyncio.create_task(yield_queue.async_q.get())

            finish, unfinished = await asyncio.wait(
                [it_task, iterator_future], return_when=asyncio.FIRST_COMPLETED
            )

            finish_condition: BaseException | None = None

            for task in finish:
                if task == iterator_future:
                    if task.exception():
                        finish_condition = task.exception()

                elif task == it_task:
                    result = task.result()
                    if isinstance(result, KoilStopIteration):
                        finish_condition = result
                    else:
                        yield_queue.async_q.task_done()
                        args = yield result
                        await next_queue.async_q.put(args)

            if finish_condition:
                yield_queue.close()
                next_queue.close()
                await next_queue.wait_closed()
                await yield_queue.wait_closed()
                try:
                    raise finish_condition
                except KoilStopIteration:
                    break
                except ThreadCancelledError:
                    break
    except asyncio.CancelledError as e:
        cancel_event.set()

        finish, unfinished = await asyncio.wait(
            [it_task, iterator_future], return_when=asyncio.FIRST_COMPLETED
        )

        raise e
