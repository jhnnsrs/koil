class KoilError(Exception):
    pass


class KoilStopIteration(Exception):
    """Wrapper around StopIteration as it interacts badly with Futures"""


class ContextError(KoilError):
    pass


class ThreadCancelledError(KoilError):
    """Mimics ayncio.CancelledError for threads if they accept cancellation. This should only
    be raised from within a thread that was spawned by koil."""


class ProcessCancelledError(KoilError):
    """Mimics ayncio.CancelledError for process if they accept cancellation. This should only
    be raised from within a thread that was spawned by koil."""


class CancelledError(KoilError):
    """Mimics ayncio.CancelledError for futures that are living in a koiled loop. Catch this
    as if you would catch asyncio.CancelledError."""


class KoilTimeoutError(KoilError, TimeoutError):
    """A koil bridge call exceeded its timeout.

    Raised by the ``*_with_timeout`` bridge variants and by
    :meth:`~koil.utils.KoilFuture.result` when a ``timeout`` is given. The
    underlying task is signalled to cancel (cooperatively, bounded by
    :attr:`~koil.loop.Koil.cancel_timeout`) before this is raised.

    Inherits the builtin :class:`TimeoutError` (which on Python >= 3.11 is also
    ``asyncio.TimeoutError`` and ``concurrent.futures.TimeoutError``), so a
    plain ``except TimeoutError`` catches it too.
    """
