import asyncio
from contextlib import contextmanager
import os
import sys
import threading
from typing import Optional, Type
from koil.vars import *
import time


class KoilTask:
    def __init__(
        self, future=None, loop=None, *args, log_errors=True, **kwargs
    ) -> None:
        super().__init__()
        self.future = future
        self.log_errors = log_errors
        self.loop = loop

        self.task = None

    async def wrapped_future(self, future):
        try:
            return await future
        except Exception as e:
            raise e

    def run(self):
        self.task = self.loop.create_task(self.wrapped_future(self.future))
        return self

    async def acancel(self):
        try:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError as e:
                print("Cancelled KoilTask")
        except Exception as e:
            print("Koil Task Cancellation failed")

    def cancel(self):
        return asyncio.run_coroutine_threadsafe(self.acancel(), self.loop).result()

    def result(self):
        return asyncio.run_coroutine_threadsafe(self.acancel(), self.loop).result()


@contextmanager
def _selector_policy():
    original_policy = asyncio.get_event_loop_policy()
    try:
        if (
            sys.version_info >= (3, 8)
            and os.name == "nt"
            and hasattr(asyncio, "WindowsSelectorEventLoopPolicy")
        ):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        yield
    finally:
        asyncio.set_event_loop_policy(original_policy)


def run_threaded_event_loop(loop):
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        try:
            # mimic asyncio.run() behavior
            # cancel unexhausted async generators
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()

            async def gather():
                await asyncio.gather(*tasks, return_exceptions=True)

            loop.run_until_complete(gather())
            for task in tasks:
                if task.cancelled():
                    continue
                if task.exception() is not None:
                    loop.call_exception_handler(
                        {
                            "message": "unhandled exception during loop shutdown",
                            "exception": task.exception(),
                            "task": task,
                        }
                    )
            if hasattr(loop, "shutdown_asyncgens"):
                loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def get_threaded_loop(name="KoilLoop"):
    """Create or return the default Koil IO loop
    The loop will be running on a separate thread.
    """
    with _selector_policy():
        newloop = asyncio.new_event_loop()

    th = threading.Thread(target=run_threaded_event_loop, args=(newloop,), name=name)
    th.daemon = True
    th.start()

    return newloop


class Koil:
    def __init__(
        self,
        name="Koiloop",
        grace_period=None,
        task_class: Optional[Type[KoilTask]] = None,
    ) -> None:
        self.it = "it"
        self.name = name
        self.task_class = task_class
        self.loop = None

    def __enter__(self):
        loop = current_loop.get()
        current_taskclass.set(
            self.task_class or current_taskclass.get(KoilTask)
        )  # task classes can be overwriten, as they only apply to the context
        if loop is not None:
            # already runnning with a koiled loop, we will just attach to it
            return self

        self.loop = get_threaded_loop(self.name)
        current_loop.set(self.loop)
        return self

    async def aclose(self):
        loop = asyncio.get_event_loop()
        print("Causing loop to stop")
        loop.stop()
        print("Loop Stopped")

    def __exit__(self, exc_type, exc_val, exc_tb):

        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.aclose(), self.loop)

            iterations = 0

            while self.loop.is_running():
                time.sleep(0.001)
                iterations += 1
                if iterations == 100:
                    print(
                        "Shutting Down takes longer than expected. Probably we are having loose Threads? Keyboard interrupt?"
                    )

            current_loop.set(None)
            current_taskclass.set(self.task_class)
