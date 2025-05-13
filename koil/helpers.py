import asyncio
import threading

import janus
from koil.errors import (
    KoilError,
    KoilStopIteration,
    ThreadCancelledError,
)
from typing import AsyncGenerator, cast
from koil.vars import (
    current_cancel_event,
    global_koil,
    global_koil_loop,
)
import contextvars
import logging
from typing import Callable, Dict, Tuple, TypeVar
from koil.utils import run_async_sharing_context, KoilFuture


try:
    from typing import ParamSpec
except ImportError:
    from typing_extensions import ParamSpec


from typing import Coroutine, Any, Union, Awaitable, Generator

P = ParamSpec("P")
T = TypeVar("T")
R = TypeVar("R")

SendType = TypeVar("SendType")




def get_koiled_loop_or_raise() -> asyncio.AbstractEventLoop:
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
    
    return koil_loop



def unkoil_gen(
    iterator: Callable[P, AsyncGenerator[R, SendType]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> Generator[R, SendType, None]:
    koil_loop = get_koiled_loop_or_raise()


    ait = iterator(*args, **kwargs).__aiter__()


    future = run_async_sharing_context(ait.__anext__, koil_loop, None)
    send_val = yield future.result()
    while True:
        if send_val is None:
            future = run_async_sharing_context(ait.__anext__, koil_loop, None)
        else:
            future = run_async_sharing_context(ait.__anext__, koil_loop, None, send_val) # type: ignore

        try:
            send_val = yield future.result()
        except StopAsyncIteration:
            break


def unkoil(
    coro: Union[
        Callable[P, Coroutine[Any, Any, R]],
        Callable[P, Awaitable[R]],
    ],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    koil_loop = get_koiled_loop_or_raise()

    context_aware_future = run_async_sharing_context(
        coro, koil_loop, None, *args, **kwargs)
    
    return context_aware_future.result()

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


def unkoil_task(
    coro: Union[
        Callable[P, Coroutine[Any, Any, R]],
        Callable[P, Awaitable[R]],
    ],
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[R]:
    koil_loop = get_koiled_loop_or_raise()

    return run_async_sharing_context(
        coro,
        koil_loop,
        None,
        *args,
        **kwargs,
    )



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

        for ctx, value in context.items():
            ctx.set(value)
            
        
        # This needs to happend after the context is set
        global_koil.set(koil)
        global_koil_loop.set(loop)
        current_cancel_event.set(cancel_event)

        return sync_func(*sync_args, **sync_kwargs) # type: ignore

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

        for ctx, value in context.items():
            ctx.set(value)
                
                
         # This needs to happend after the context is set
        global_koil.set(koil)
        global_koil_loop.set(loop)
        current_cancel_event.set(cancel_event)
        

        generator = cast(Generator[R, S, None], sync_gen(*sync_args, **sync_kwargs))  # type: ignore
        it = generator.__iter__()

        args = ()
        while True:
            try:
                res = it.__next__(*args if args else ())
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

    it_task: asyncio.Task[R] | None = None

    try:
        while True:
            it_task = asyncio.create_task(yield_queue.async_q.get())

            finish, _ = await asyncio.wait(
                [it_task, iterator_future], return_when=asyncio.FIRST_COMPLETED
            )

            finish_condition: BaseException | None = None

            for task in finish:
                if task == iterator_future:
                    typed_task = cast(asyncio.Task[None], task)
                    if task.exception():
                        finish_condition = task.exception()

                elif task == it_task:
                    typed_task = cast(asyncio.Task[R], task)
                    result = typed_task.result()
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
        
        yield_queue.close()
        next_queue.close()
        
        await next_queue.wait_closed()
        await yield_queue.wait_closed()

        if not it_task:
            await asyncio.wait_for(
                iterator_future, timeout=koil.cancel_timeout if koil else 2
            )
        else:
            finish, _ = await asyncio.wait(
                [it_task, iterator_future], return_when=asyncio.FIRST_COMPLETED
            )

        raise e
