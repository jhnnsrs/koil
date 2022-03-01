from calendar import c
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
from koil.vars import *


class KoiledExecutor(ThreadPoolExecutor):
    def submit(self, fn, *args, cancel_event=None, **kwargs):
        copy_loop = asyncio.get_event_loop()
        copy_loop.run_in_executor()

        assert (
            copy_loop.is_running()
        ), "Loop is not running. You shouldn't be using this"

        def wrap(*args, **kwargs):
            current_loop.set(copy_loop)
            current_cancel_event.set(cancel_event)
            t = fn(*args, **kwargs)
            current_loop.set(None)
            current_cancel_event.set(None)
            return t

        return super().submit(wrap, *args, **kwargs)

    async def asubmit(self, fn, *args, **kwargs):

        cancel_event = threading.Event()

        try:
            return await asyncio.wrap_future(
                super().submit(fn, *args, **kwargs, cancel_event=cancel_event)
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise
