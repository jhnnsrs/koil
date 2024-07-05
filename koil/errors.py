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
