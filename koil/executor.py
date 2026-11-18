"""The thread pool :func:`~koil.bridge.run_threaded` runs sync code on."""

import concurrent.futures
import itertools
import queue
import threading
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

_WorkItem = tuple["concurrent.futures.Future[Any]", Callable[..., Any], Any, Any]

_pool_counter = itertools.count()


class KoilExecutor(concurrent.futures.Executor):
    """A thread pool with daemon threads, owned by one :class:`~koil.loop.Koil`.

    Two things set it apart from the event loop's default executor:

    * It is koil's own. The default executor is small (``min(32, cpu + 4)``
      threads) and shared with the loop's own blocking work, name resolution
      (``getaddrinfo``) above all: a handful of long-running sync functions on
      it stall every coroutine that needs to open a connection.
    * Its threads are daemons, like the loop thread. A worker that never
      acknowledges cancellation is abandoned when its :class:`~koil.loop.Koil`
      exits, instead of blocking interpreter shutdown forever (the threads of a
      :class:`~concurrent.futures.ThreadPoolExecutor` are joined at exit).

    Threads are started on demand, up to *max_workers*, and reused.
    """

    def __init__(self, max_workers: int, thread_name_prefix: str = "KoilWorker") -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._max_workers = max_workers
        self._prefix = f"{thread_name_prefix}-{next(_pool_counter)}"
        self._work: "queue.SimpleQueue[_WorkItem | None]" = queue.SimpleQueue()
        self._idle = threading.Semaphore(0)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown = False

    def submit(
        self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> "concurrent.futures.Future[T]":
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: "concurrent.futures.Future[T]" = concurrent.futures.Future()
            self._work.put((future, fn, args, kwargs))
            # An idle thread will pick the item up; only start another when
            # every existing one is busy.
            if not self._idle.acquire(blocking=False):
                if len(self._threads) < self._max_workers:
                    thread = threading.Thread(
                        target=self._run,
                        name=f"{self._prefix}_{len(self._threads)}",
                        daemon=True,
                    )
                    self._threads.append(thread)
                    thread.start()
            return future

    def _run(self) -> None:
        __tracebackhide__ = True  # transport, like the stdlib pool's frames
        while True:
            item = self._work.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            if future.set_running_or_notify_cancel():
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            # Don't keep the call (its arguments, its result) alive while idle.
            del item, future, fn, args, kwargs
            self._idle.release()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                threads = list(self._threads)
            else:
                self._shutdown = True
                if cancel_futures:
                    while True:
                        try:
                            item = self._work.get_nowait()
                        except queue.Empty:
                            break
                        if item is not None:
                            item[0].cancel()
                threads = list(self._threads)
                for _ in threads:
                    self._work.put(None)
        if wait:
            for thread in threads:
                thread.join()
