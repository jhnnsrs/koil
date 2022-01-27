import threading
import asyncio
from asyncio.runners import _cancel_all_tasks
from threading import Thread
import os
import logging
import time

from koil.checker.registry import get_checker_registry
from koil.state import KoilState

logger = logging.getLogger(__name__)


def newloop(loop, loop_started):
    asyncio.set_event_loop(loop)
    try:
        loop_started.set()
        logger.info("Running New Event loop in another Thread")
        loop.run_forever()
    finally:
        logger.info("Loop Shutting Down")
        try:
            _cancel_all_tasks(loop)
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class Koil:
    def __init__(
        self,
        force_sync=False,
        force_async=False,
        register_default_checkers=True,
        **overrides,
    ) -> None:
        """Creates A Herre Client

        Args:
            config_path (str, optional): [description]. Defaults to "bergen.yaml".
            username (str, optional): [description]. Defaults to None.
            password (str, optional): [description]. Defaults to None.
            allow_insecure (bool, optional): [description]. Defaults to False.
            in_sync (bool, optional): Should we force an in_sync modus if an event loop is already running. Loop will be send to another thread. Defaults to True.

        Raises:
            HerreError: [description]
        """

        self.loop = None
        self.thread_id = None
        self.state = get_checker_registry(
            register_defaults=register_default_checkers
        ).get_desired_state(self)

        if force_sync or force_async:
            self.state.threaded = (
                force_sync and not force_async
            )  # Force async has priority

        if self.state.threaded:
            self.loop = asyncio.new_event_loop()
            self.loop_started_event = threading.Event()
            self.thread = Thread(
                target=newloop, args=(self.loop, self.loop_started_event)
            )
            self.thread.start()
            self.loop_started_event.wait()
            logger.info("Running in Seperate Thread so that we can use the sync syntax")
        else:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError as e:
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)

        set_current_koil(self)

    async def aclose(self):
        loop = asyncio.get_event_loop()

    def close(self):
        # Do according to state
        if self.state.threaded:
            self.loop.call_soon_threadsafe(self.loop.stop())

            while self.loop.is_running():
                logger.info("Waiting for the Loop to close")
                time.sleep(0.1)


class KoiledContext:
    def __init__(self) -> None:
        pass

    def __enter__(self):
        self.koil = Koil(force_sync=True)
        return self.koil

    def __exit__(self, *args, **kwargs):
        self.koil.close()
        return

    async def __aenter__(self):
        self.koil = Koil(force_async=True)
        return self.koil

    async def __aexit__(self, *args, **kwargs):
        await self.koil.aclose()
        self.koil = None


koiled = KoiledContext()

CURRENT_KOIL = None


def get_current_koil(**kwargs):
    global CURRENT_KOIL
    if not CURRENT_KOIL:
        CURRENT_KOIL = Koil(**kwargs)
    return CURRENT_KOIL


def set_current_koil(koil):
    global CURRENT_KOIL
    CURRENT_KOIL = koil
