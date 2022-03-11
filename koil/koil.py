import asyncio
from contextlib import contextmanager
import os
import sys
import threading
from typing import Optional, Type
from koil.errors import ContextError
from koil.vars import *
from koil.task import KoilGeneratorTask, KoilTask
import time
import logging


logger = logging.getLogger(__name__)

try:
    import uvloop
except:
    uvloop = None


@contextmanager
def _selector_policy(uvify=True):

    original_policy = asyncio.get_event_loop_policy()

    try:
        if uvify:
            if uvloop:
                asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            else:
                logger.info("uvloop not installed, using default policy")
        elif (
            sys.version_info >= (3, 8)
            and os.name == "nt"
            and hasattr(asyncio, "WindowsSelectorEventLoopPolicy")
        ):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        yield
    finally:
        asyncio.set_event_loop_policy(original_policy)


def run_threaded_event_loop(loop):

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
                logger.debug(f"Cancelling {tasks}")
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
            logger.debug("Loop Sucessfully Closed")
            loop.close()


def get_threaded_loop(name="KoilLoop", uvify=True):
    """Creates a new event loop and run it in a new thread."""
    with _selector_policy(uvify=uvify):
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
        uvify=True,
        task_class: Optional[Type[KoilTask]] = None,
        gen_class: Optional[Type[KoilGeneratorTask]] = None,
    ) -> None:
        self.it = "it"
        self.name = name
        self.task_class = task_class
        self.gen_class = gen_class
        self.loop = None
        self.popped_loop = None
        self.uvify = uvify

    async def __aenter__(self):
        self.old_loop = current_loop.get()
        loop = asyncio.get_event_loop()
        current_loop.set(loop)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        current_loop.set(self.old_loop)

    def __enter__(self):
        try:
            asyncio.get_running_loop()
            raise ContextError(
                "You are running in an event loop already. Using koil makes no sense here, use asyncio instead. If this happens in a context manager, you probably forgot to use the `async with` syntax."
            )
        except RuntimeError:
            pass

        self.old_loop = current_loop.get()
        self.old_taskclass = current_taskclass.get()
        self.old_genclass = current_genclass.get()

        current_taskclass.set(
            self.task_class or self.old_taskclass or KoilTask
        )  # task classes can be overwriten, as they only apply to the context
        current_genclass.set(
            self.gen_class or self.old_genclass or KoilGeneratorTask
        )  # task classes can be overwriten, as they only apply to the context
        if self.old_loop is not None:
            # already runnning with a koiled loop, we will just attach to it
            return self

        self.popped_loop = get_threaded_loop(self.name, uvify=self.uvify)
        current_loop.set(self.popped_loop)
        return self

    async def aclose(self):
        loop = asyncio.get_event_loop()
        logger.debug("Causing loop to stop")
        loop.stop()

    def __exit__(self, exc_type, exc_val, exc_tb):

        if self.popped_loop is not None:
            asyncio.run_coroutine_threadsafe(self.aclose(), self.popped_loop)

            iterations = 0

            while self.popped_loop.is_running():
                time.sleep(0.001)
                iterations += 1
                if iterations == 100:
                    logger.warning(
                        "Shutting Down takes longer than expected. Probably we are having loose Threads? Keyboard interrupt?"
                    )

            current_loop.set(self.old_loop)  # Reset the loop

        current_taskclass.set(self.old_taskclass)
        current_taskclass.set(self.old_genclass)
