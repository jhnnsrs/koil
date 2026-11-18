"""Internal utilities: introspection helpers, future wrappers, and context runners.

The central type here is :class:`KoilFuture`, which wraps a
:class:`concurrent.futures.Future` with a koil-aware cancel mechanism.
:func:`run_async_sharing_context` and :func:`iterate_async_sharing_context`
are the low-level primitives that submit coroutines/generators to the koil
loop while propagating the caller's :class:`~contextvars.Context` and
cancellation event; :class:`AsyncGenDriver` does the same for an async
generator a sync consumer steps through.
"""
import asyncio
import contextvars
import concurrent.futures
import threading
import time
from typing import (
    Any,
    Generic,
    ParamSpec,
    TypeVar,
)
from collections.abc import AsyncIterator, Awaitable, Callable
from koil.errors import CancelledError, KoilTimeoutError, ThreadCancelledError
from koil.types import AnyCallable
from koil.context import (
    INTERNAL_VARS,
    BlockedCall,
    KoilThreadSafeEvent,
    blocked_calls,
    current_cancel_event,
    global_koil,
    loop_state,
)
from koil.tracebacks import prune_traceback, should_rewrite
import inspect
import logging
from koil.protocols import TaskSignalProtocol, IteratorSignalProtocol

logger = logging.getLogger(__name__)


def check_is_asyncgen(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is an async generator function."""
    if inspect.isasyncgenfunction(func):
        return True
    return False


def check_is_asyncfunc(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is a coroutine function."""
    if inspect.iscoroutinefunction(func):
        return True
    return False


def check_is_syncgen(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is a synchronous generator function."""
    if inspect.isgeneratorfunction(func):
        return True
    return False


def check_is_syncfunc(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is a plain synchronous function."""
    if inspect.isfunction(func):
        return True
    return False


P = ParamSpec("P")
T = TypeVar("T")

#: Default interval, in seconds, at which a :meth:`KoilFuture.result` blocking
#: the *main thread* wakes up to let the interpreter deliver a pending signal
#: (e.g. a Ctrl+C SIGINT).
#:
#: Python only runs signal handlers on the main thread when it returns from a
#: C-level blocking call, so an indefinite ``Future.result()`` would defer a
#: KeyboardInterrupt until the task completes. This poll interval bounds that
#: delay. It adds *no* latency to fast tasks: the wait returns the instant the
#: future is done, so the timeout only ever fires while genuinely waiting.
#: Other threads never receive signals and block without polling.
#:
#: This is the process-wide default. Override it globally by reassigning this
#: module attribute (``koil.utils.RESULT_POLL_INTERVAL = 0.1``), or per call via
#: the ``poll_interval`` argument to :meth:`KoilFuture.result`. Smaller values
#: make Ctrl+C more responsive at the cost of slightly more wakeups while idle.
#:
#: This is intentionally *separate* from :attr:`koil.loop.Koil.cancel_timeout`,
#: which bounds how long shutdown waits for a worker to acknowledge cancellation
#: — a different concern with different tuning needs.
RESULT_POLL_INTERVAL: float = 0.05


#: Fallback grace period, in seconds, granted to a timed-out task to
#: acknowledge its cancellation when no :class:`~koil.loop.Koil` instance (and
#: therefore no :attr:`~koil.loop.Koil.cancel_timeout`) is ambient. Matches the
#: fallback used by :func:`koil.bridge.run_threaded` on cancellation.
CANCEL_GRACE_FALLBACK: float = 10.0


#: Extra seconds a ``*_with_timeout`` caller waits past the loop-side deadline
#: (timeout plus cancellation grace) before giving up on its own. The deadline
#: is a timer on the loop; a loop wedged on blocking code never fires it.
TIMEOUT_BACKSTOP_MARGIN: float = 1.0


def timeout_backstop(timeout: float) -> float:
    """How long a sync caller waits for a call the loop bounds by *timeout*."""
    return timeout + _cancel_grace(global_koil.get()) + TIMEOUT_BACKSTOP_MARGIN


def _cancel_grace(koil: object) -> float:
    """The cancellation grace period for *koil*, or the module fallback."""
    value = getattr(koil, "cancel_timeout", None) if koil is not None else None
    return CANCEL_GRACE_FALLBACK if value is None else value


#: Bounded time, in seconds, that :func:`aclose_async_gen_threadsafe` waits for a
#: generator's :meth:`~agen.aclose` to finish running on the koil loop before
#: giving up. Teardown should be prompt; an aclose that takes longer than this
#: is almost certainly wedged on uncancellable work, and blocking forever would
#: defeat the point of a cooperative close.
ACLOSE_TIMEOUT: float = 5.0


def aclose_async_gen_threadsafe(
    agen: object,
    loop: asyncio.AbstractEventLoop,
    timeout: float | None = None,
) -> None:
    """Close an async generator on *loop* from another thread, tolerating races.

    Drives ``agen.aclose()`` on the koil loop so the generator's ``finally``
    blocks run promptly and deterministically — instead of being deferred to
    garbage collection or :meth:`asyncio.AbstractEventLoop.shutdown_asyncgens`
    at loop close.

    The tricky case this guards is a *concurrent* teardown: if the generator's
    ``__anext__`` is still unwinding from a cancellation when we call
    ``aclose()``, CPython raises ``RuntimeError("aclose(): asynchronous
    generator is already running")`` — two teardown paths cannot drive one
    generator frame at once. When that happens the *other* path is already
    closing the generator (its ``finally`` will still run exactly once), so the
    error is benign and is swallowed here.

    Safe to call on a non-generator (no ``aclose`` -> no-op) and on an
    already-closed loop (the work is necessarily finished, so the close is a
    no-op).

    Args:
        agen: An async generator (or async iterator). Anything without an
            ``aclose`` attribute is ignored.
        loop: The koil loop the generator lives on.
        timeout: Seconds to wait for the close to complete. Defaults to
            :data:`ACLOSE_TIMEOUT`.
    """
    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    if loop.is_closed():
        return

    # If we are *on* the target loop (e.g. this runs from a loop callback
    # because the generator was finalized there), we must not block on
    # run_coroutine_threadsafe(...).result() — the loop can't run the close
    # while it's blocked waiting on it. Schedule a fire-and-forget close
    # instead; its finally still runs on the next loop iteration.
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is loop:
        loop.create_task(aclose())
        return

    try:
        close_future = asyncio.run_coroutine_threadsafe(aclose(), loop)
    except RuntimeError:
        # Loop stopped/closed between the is_closed() check and submission;
        # nothing left to close.
        return

    try:
        close_future.result(timeout=ACLOSE_TIMEOUT if timeout is None else timeout)
    except RuntimeError:
        # "aclose(): asynchronous generator is already running" — another
        # teardown path (a cancelled __anext__ unwinding, or the loop's
        # shutdown_asyncgens) is already closing this generator. Its finally
        # still runs exactly once; nothing to do here.
        pass
    except (CancelledError, concurrent.futures.CancelledError):
        pass
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Timed out after %.1fs closing async generator %r on the koil loop; "
            "it may be wedged on uncancellable work.",
            ACLOSE_TIMEOUT if timeout is None else timeout,
            agen,
        )


def _safe_set_event(event: Any) -> None:
    """Set *event*, tolerating a closed event loop.

    A foreign event that routes through ``call_soon_threadsafe`` raises
    :class:`RuntimeError` when the koil loop is already closed. If the loop is
    gone the work is already finished/cancelled, so swallowing that error is
    correct.
    """
    try:
        event.set()
    except RuntimeError:
        pass


_MISSING: Any = object()


def _context_delta(
    after: contextvars.Context, baseline: contextvars.Context | None
) -> list[tuple[contextvars.ContextVar[Any], Any]]:
    """The ContextVars whose value in *after* differs from *baseline*.

    This is what a bridged call publishes back to its caller: only what the
    call itself changed. Copying every variable back would also overwrite
    whatever the caller changed while the call was running (a
    :func:`~koil.bridge.unkoil_task` caller keeps going), and re-setting
    hundreds of unchanged variables on every call is wasted work. Koil's own
    bookkeeping variables are never published. Without a *baseline* every
    variable counts as changed.
    """
    return [
        (var, value)
        for var, value in after.items()
        if var not in INTERNAL_VARS
        and (baseline is None or baseline.get(var, _MISSING) is not value)
    ]


def _apply_context(source: contextvars.Context) -> None:
    """Make the current context agree with *source* on every variable *source* holds."""
    for var, value in source.items():
        if var.get(_MISSING) is not value:
            var.set(value)


def _call_threadsafe(
    loop: asyncio.AbstractEventLoop, callback: Callable[..., Any], *args: Any
) -> None:
    """``loop.call_soon_threadsafe`` that tolerates a closed loop.

    Used for cancel requests: if the loop is gone the work is already
    finished or cancelled, so there is nothing left to ask.
    """
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:
        pass


def _watch(
    event: Any,
    loop: asyncio.AbstractEventLoop,
    callback: Callable[..., Any],
    *args: Any,
) -> Callable[[], None]:
    """Run ``callback(*args)`` on *loop* once *event* is set; returns the unsubscribe.

    A foreign event object (anything without ``add_callback``) is only checked
    before a call is submitted, not while it is in flight.
    """
    add_callback = getattr(event, "add_callback", None)
    if add_callback is None:
        return _noop
    return add_callback(lambda: _call_threadsafe(loop, callback, *args))  # type: ignore[no-any-return]


def _noop() -> None:
    return None


def _emit(signal: Any, *args: Any) -> None:
    """Emit a lifecycle signal; a failing receiver must not cost the caller its result."""
    try:
        signal.emit(*args)
    except Exception:
        logger.warning("Emitting %r failed", signal, exc_info=True)


def _settle(
    future: "concurrent.futures.Future[Any]",
    *,
    value: Any = _MISSING,
    error: BaseException | None = None,
) -> bool:
    """Resolve *future* unless something else already did; returns whether we did."""
    try:
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)
    except concurrent.futures.InvalidStateError:
        return False
    return True


def _reason_error(
    reason: str | None, timeout: float | None, noun: str, acknowledged: bool = True
) -> BaseException:
    """The exception a caller sees for a call that was cancelled for *reason*."""
    if reason == "timeout":
        verb = "finish" if noun == "Generator" else "complete"
        if acknowledged:
            return KoilTimeoutError(
                f"{noun} did not {verb} within {timeout}s; it was cancelled."
            )
        return KoilTimeoutError(
            f"{noun} did not {verb} within {timeout}s and did not "
            f"acknowledge cancellation within the grace period. Make "
            f"sure the task is not blocking the loop with "
            f"uncancellable work and that workers call "
            f"check_cancelled periodically."
        )
    if reason == "ambient":
        # The worker thread making the call was cancelled: the same error
        # every other cancellation check in that worker raises.
        return ThreadCancelledError("Thread was cancelled")
    if reason == "cancel":
        return CancelledError("Future was cancelled")
    return CancelledError("The call was cancelled on the koil loop")


def _waiter(future: "concurrent.futures.Future[Any]") -> threading.Event:
    """An event that is set once *future* is done, for a thread to wait on.

    Deliberately not :func:`concurrent.futures.wait`: that helper takes the
    future's own lock in plain Python code, and a KeyboardInterrupt landing
    right there (this is where the main thread sits while it waits for Ctrl+C)
    leaves the lock held for good. The loop thread then blocks forever trying
    to resolve the future, and with it every later call. Waiting on a private
    event keeps an interrupt away from any lock the loop needs.
    """
    done = threading.Event()
    future.add_done_callback(lambda _: done.set())
    return done


def _track(
    loop: asyncio.AbstractEventLoop, future: "concurrent.futures.Future[Any]"
) -> None:
    """Have *future* failed, instead of left hanging, if *loop* stops before resolving it."""
    state = loop_state(loop)
    if state is not None:
        state.track(future)


class _KoilFutureBase(Generic[T]):
    """Shared implementation for :class:`KoilFuture` and :class:`KoilIterator`.

    Wraps a :class:`concurrent.futures.Future` with a koil-aware cancel
    mechanism: cancellation is signalled via a
    :class:`~koil.context.KoilThreadSafeEvent` rather than by attempting to
    interrupt the underlying thread directly.
    """

    def __init__(
        self,
        future: concurrent.futures.Future[tuple[T, contextvars.Context]],
        cancel_event: KoilThreadSafeEvent,
        baseline: contextvars.Context | None = None,
        blocked_call: BlockedCall | None = None,
    ) -> None:
        super().__init__()
        self.future = future
        self.cancel_event = cancel_event
        self._baseline = baseline
        self._blocked_call = blocked_call
        self._cancel_requested = False
        self._cancel_reason: str | None = None
        self._timed_out = False

    def cancel(self, reason: str | None = None) -> bool:
        """Request cancellation of the running task.

        Sets the associated cancel event, which cancels the coroutine on the
        koil loop. Cancelling a future affects that future only.

        Args:
            reason: Optional human-readable explanation of why cancellation was
                requested (e.g. ``"keyboard interrupt"``). Recorded on the
                future as :attr:`cancel_reason` and logged, so the origin of a
                cancellation can be traced after the fact.

        Returns:
            ``True`` if the task was still running at the time of the request;
            ``False`` if it had already completed.
        """
        if not self.future.done():
            self._cancel_requested = True
            self._cancel_reason = reason
            logger.debug("Cancelling %r (reason: %s)", self, reason or "unspecified")
            _safe_set_event(self.cancel_event)
            return True
        return False

    @property
    def cancel_reason(self) -> str | None:
        """The ``reason`` passed to the most recent :meth:`cancel` call, if any."""
        return self._cancel_reason

    def done(self) -> bool:
        """Return ``True`` if the underlying future has completed."""
        assert self.future, "Task was never run! Please run task before"
        return self.future.done()

    def cancelled(self) -> bool:
        """Return ``True`` if cancellation has been requested or confirmed."""
        assert self.future, "Task was never run! Please run task before"
        return self._cancel_requested or self.cancel_event.is_set()

    def _timeout_expired(self, timeout: float) -> KoilTimeoutError:
        """Handle an expired :meth:`result` deadline; returns the error to raise.

        Signals cooperative cancellation via the existing cancel machinery,
        then grants the task the ambient :attr:`~koil.loop.Koil.cancel_timeout`
        grace to acknowledge before giving up on it.
        """
        __tracebackhide__ = True
        self._timed_out = True
        self.cancel(reason=f"timeout after {timeout}s")
        grace = _cancel_grace(global_koil.get())
        if not _waiter(self.future).wait(grace):
            return KoilTimeoutError(
                f"Task did not complete within {timeout}s and did not "
                f"acknowledge cancellation within the {grace}s grace period. "
                f"Make sure the task is not blocking the loop with "
                f"uncancellable work and that workers call check_cancelled "
                f"periodically."
            )
        return KoilTimeoutError(
            f"Task did not complete within {timeout}s; it was cancelled."
        )

    def result(
        self, timeout: float | None = None, poll_interval: float | None = None
    ) -> T:
        """Block until the result is available and return it.

        Also publishes the :class:`~contextvars.ContextVar` values the task
        changed back into the calling context.

        On the main thread the wait is performed in short slices rather than
        as one indefinite block, so that a Ctrl+C (SIGINT) is delivered
        promptly instead of being swallowed until the coroutine finishes on
        the background loop. Signal handlers only ever run on the main thread,
        so every other thread simply blocks until the task is done.

        Args:
            timeout: Maximum seconds to wait for the result. On expiry the task
                is signalled to cancel (cooperatively, with the ambient
                :attr:`~koil.loop.Koil.cancel_timeout` as acknowledgement
                grace) and :class:`~koil.errors.KoilTimeoutError` is raised.
                ``None`` (the default) waits indefinitely. ``0`` requires the
                task to already be done.
            poll_interval: Seconds to wait per slice before re-checking for a
                pending signal. Defaults to the module-wide
                :data:`RESULT_POLL_INTERVAL` on the main thread; passing it
                makes any thread wait in slices.

        Raises:
            KoilTimeoutError: If *timeout* elapses before the task completes.
            ValueError: If *timeout* is negative.
            CancelledError: If the task was cancelled, or the koil loop shut
                down before it finished.
            ThreadCancelledError: If the worker thread that made the call was
                cancelled while the task was running.
            KeyboardInterrupt: If the caller is interrupted while waiting. The
                in-flight task is *signalled* to cancel (cooperatively) before
                the interrupt is re-raised; it is not joined, so it may still be
                unwinding on the background thread when control returns.
            Any exception raised by the coroutine/function.
        """
        __tracebackhide__ = True
        assert self.future, "Task was never run! Please run task before"
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        interval: float | None
        if poll_interval is not None:
            interval = poll_interval
        elif threading.current_thread() is threading.main_thread():
            interval = RESULT_POLL_INTERVAL
        else:
            interval = None
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            # Wait in bounded slices so a pending Ctrl+C on the main thread is
            # delivered between slices instead of deferred until the task
            # completes. Wait on an event (see _waiter) rather than
            # result(timeout=...): on 3.11 concurrent.futures.TimeoutError
            # *is* the builtin TimeoutError, so catching a result() timeout would
            # also swallow a real TimeoutError raised by the coroutine and spin
            # forever. The event wait never raises, so the subsequent result()
            # re-raises the coroutine's actual exception (TimeoutError included).
            # A done() check always precedes the deadline check, so a completed
            # future returns its result even with timeout=0.
            done = None if self.future.done() else _waiter(self.future)
            while done is not None and not done.is_set():
                slice_timeout = interval
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._timeout_expired(timeout)  # type: ignore[arg-type]
                    slice_timeout = (
                        remaining if interval is None else min(interval, remaining)
                    )
                done.wait(slice_timeout)
            res, context = self.future.result()
        except CancelledError:
            raise
        except KeyboardInterrupt:
            # The coroutine is still running on the background loop. Signal
            # cooperative cancellation so it unwinds instead of being orphaned,
            # then propagate the interrupt to the caller without waiting.
            self.cancel(reason="keyboard interrupt")
            raise
        except BaseException as e:
            # This frame is both a pruning boundary and marker-hidden: a direct
            # unkoil_task(...).result() caller keeps exactly this one koil
            # frame, while a boundary one level up (unkoil) prunes it away
            # again, leaving its own frame as the single marker.
            if should_rewrite():
                prune_traceback(e, keep_boundary=True)
            raise
        finally:
            # Whatever happened, this thread is no longer blocked on the call.
            if self._blocked_call is not None:
                self._blocked_call.active = False

        for var, value in _context_delta(context, self._baseline):
            var.set(value)

        return res


class KoilFuture(_KoilFutureBase[T]):
    """A cancellable future representing a single coroutine submitted to the koil loop.

    Returned by :func:`~koil.bridge.unkoil_task` and used internally by
    :func:`~koil.utils.run_async_sharing_context`. Calling :meth:`cancel`
    cancels the coroutine on the loop; :meth:`result` then raises
    :class:`~koil.errors.CancelledError`.
    """


class KoilIterator(_KoilFutureBase[T]):
    """A cancellable future representing an async iterator submitted to the koil loop.

    Shares the cancellation semantics of :class:`KoilFuture`.
    """


class _Call:
    """Loop-side state of one coroutine submitted from a sync thread.

    The coroutine runs as a single task. Everything that can end it early —
    the caller cancelling its future, the caller's worker thread being
    cancelled, a deadline — is a *request* made on the loop that cancels that
    one task, and the task's done-callback resolves the caller's future. All
    methods run on the loop.
    """

    __slots__ = (
        "loop",
        "future",
        "make",
        "context",
        "timeout",
        "grace",
        "signals",
        "finished",
        "noun",
        "blocked_call",
        "task",
        "reason",
        "timer",
        "grace_timer",
        "unwatch",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        future: "concurrent.futures.Future[Any]",
        make: Callable[[], Awaitable[Any]],
        context: contextvars.Context,
        timeout: float | None,
        grace: float,
        signals: Any,
        finished: Any,
        noun: str,
        blocked_call: BlockedCall | None,
    ) -> None:
        self.loop = loop
        self.future = future
        self.make = make
        self.context = context
        self.timeout = timeout
        self.grace = grace
        self.signals = signals
        self.finished = finished
        self.noun = noun
        self.blocked_call = blocked_call
        self.task: asyncio.Task[Any] | None = None
        self.reason: str | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.grace_timer: asyncio.TimerHandle | None = None
        self.unwatch: list[Callable[[], None]] = []

    async def _run(self) -> tuple[Any, contextvars.Context]:
        __tracebackhide__ = True
        result = await self.make()
        return result, contextvars.copy_context()

    def start(self) -> None:
        try:
            # Created inside the caller's context: the task runs in a copy of
            # it, so the coroutine reads the ContextVars the calling thread set.
            self.task = self.context.run(self.loop.create_task, self._run())
        except BaseException as e:
            self._finish(error=e)
            return
        if self.blocked_call is not None:
            self.blocked_call.task = self.task
        self.task.add_done_callback(self._on_done)
        if self.timeout is not None:
            self.timer = self.loop.call_later(self.timeout, self.request, "timeout")

    def request(self, reason: str) -> None:
        """Cancel the task for *reason* (``cancel``, ``ambient`` or ``timeout``). First one wins."""
        task = self.task
        if task is None or task.done() or self.future.done() or self.reason is not None:
            return
        self.reason = reason
        task.cancel()
        if reason == "timeout":
            # A coroutine that swallows its cancellation must not wedge the
            # caller forever: after the grace period the caller gets its
            # timeout and the task is left behind.
            self.grace_timer = self.loop.call_later(self.grace, self._abandon)

    def _abandon(self) -> None:
        error = _reason_error("timeout", self.timeout, self.noun, acknowledged=False)
        if self._finish(error=error) and self.signals:
            _emit(self.signals.errored, error)

    def _finish(self, *, value: Any = _MISSING, error: BaseException | None = None) -> bool:
        for handle in (self.timer, self.grace_timer):
            if handle is not None:
                handle.cancel()
        for unwatch in self.unwatch:
            unwatch()
        if self.blocked_call is not None:
            self.blocked_call.active = False
        return _settle(self.future, value=value, error=error)

    def _on_done(self, task: "asyncio.Task[Any]") -> None:
        if self.future.done():
            # Abandoned after a timeout nobody acknowledged; just collect the
            # task's outcome so asyncio does not report it as never retrieved.
            if not task.cancelled():
                task.exception()
            self._finish()
            return

        if task.cancelled():
            error = _reason_error(self.reason, self.timeout, self.noun)
        elif (raised := task.exception()) is not None:
            # Prune on the loop side so signal handlers (the Qt errored
            # slots) receive a clean traceback too. The sync-side boundary
            # prunes the same exception object again — idempotent.
            if should_rewrite():
                prune_traceback(raised)
            error = raised
        elif self.reason is not None:
            # Finished, but only after it was told to stop: the request wins.
            error = _reason_error(self.reason, self.timeout, self.noun)
        else:
            result = task.result()
            if self._finish(value=result) and self.signals:
                _emit(self.finished, result if self.noun == "Task" else None)
            return

        if self._finish(error=error) and self.signals:
            # A timeout is an error, not a cancellation.
            if isinstance(error, (CancelledError, ThreadCancelledError)):
                _emit(self.signals.cancelled, error)
            else:
                _emit(self.signals.errored, error)


def _submit(
    make: Callable[[], Awaitable[Any]],
    loop: asyncio.AbstractEventLoop,
    timeout: float | None,
    signals: Any,
    finished: Any,
    noun: str,
    blocking: bool,
) -> "KoilFuture[Any]":
    """Run ``make()`` as one task on *loop*; shared by every sync → async bridge."""
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")

    # The scope-wide event injected by run_threaded. We react to it (tearing
    # down the enclosing worker cancels nested work) but never set it: what
    # KoilFuture.cancel() and the Ctrl+C path set is the per-future event
    # below, so cancelling one future cannot poison later calls in the scope.
    ambient = current_cancel_event.get()
    if ambient is not None and ambient.is_set():
        raise ThreadCancelledError("Thread was cancelled")

    baseline = contextvars.copy_context()
    context = contextvars.copy_context()
    blocked_call: BlockedCall | None = None
    chain = blocked_calls.get()
    if blocking:
        blocked_call = BlockedCall(threading.get_ident())
        context.run(blocked_calls.set, chain + (blocked_call,))
    elif chain:
        # Nobody blocks on a detached task.
        context.run(blocked_calls.set, ())

    future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
    own_cancel_event = KoilThreadSafeEvent(loop)
    call = _Call(
        loop,
        future,
        make,
        context,
        timeout,
        _cancel_grace(global_koil.get()),
        signals,
        finished,
        noun,
        blocked_call,
    )

    _track(loop, future)
    loop.call_soon_threadsafe(call.start)
    # Registered after start is scheduled, so a request can never overtake it.
    call.unwatch.append(_watch(own_cancel_event, loop, call.request, "cancel"))
    if ambient is not None:
        call.unwatch.append(_watch(ambient, loop, call.request, "ambient"))
    if future.done():
        # Finished before we were done subscribing; nothing unsubscribed us.
        for unwatch in call.unwatch:
            unwatch()

    return KoilFuture(future, own_cancel_event, baseline, blocked_call)


def run_async_sharing_context(
    coro: Callable[P, Awaitable[T]],
    loop: asyncio.AbstractEventLoop,
    signals: TaskSignalProtocol[T] | None,
    timeout: float | None = None,
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[T]:
    """Submit *coro* to *loop* while propagating context and the cancel event.

    Unlike :func:`asyncio.run_coroutine_threadsafe`, this function:

    * Runs the coroutine in a copy of the caller's
      :class:`~contextvars.Context`, so :class:`~contextvars.ContextVar` reads
      inside the coroutine see the values set by the calling thread, and hands
      the values the coroutine changed back through
      :meth:`KoilFuture.result`.
    * Cancels the coroutine when the returned future is cancelled, or when the
      worker thread making the call is (the active cancel event fires).
    * Optionally bounds the coroutine by a *timeout* enforced on the loop;
      on expiry the coroutine is cancelled (bounded by the ambient
      :attr:`~koil.loop.Koil.cancel_timeout` grace) and the future resolves
      with :class:`~koil.errors.KoilTimeoutError`.
    * Optionally emits lifecycle signals (*returned*, *errored*, *cancelled*)
      if a *signals* object is provided (used by the Qt integration).

    Args:
        coro: An async callable to invoke.
        loop: The koil event loop to run the coroutine on.
        signals: Optional signal holder for Qt integration callbacks.
        timeout: Maximum seconds the coroutine may run, or ``None`` for no
            limit. A timeout counts as an *error* (the ``errored`` signal is
            emitted), not as a cancellation. Note this parameter sits before
            ``*args``: callers forwarding user arguments positionally must pass
            it explicitly.
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        A :class:`KoilFuture` that resolves to the return value of *coro*.

    Raises:
        ValueError: If *timeout* is negative.
        ThreadCancelledError: If the current cancel event is already set when
            this function is called.
    """
    return _submit(
        lambda: coro(*args, **kwargs),
        loop,
        timeout,
        signals,
        signals.returned if signals else None,
        "Task",
        blocking=False,
    )


def run_async_blocking(
    coro: Callable[..., Awaitable[T]],
    loop: asyncio.AbstractEventLoop,
    timeout: float | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> KoilFuture[T]:
    """:func:`run_async_sharing_context` for a caller that blocks on the result right away.

    The task is told which thread waits for it, so code on the loop can refuse
    to hop to that thread instead of deadlocking.
    """
    return _submit(
        lambda: coro(*args, **kwargs), loop, timeout, None, None, "Task", blocking=True
    )


def iterate_async_sharing_context(
    coro: Callable[P, AsyncIterator[T]],
    loop: asyncio.AbstractEventLoop,
    signals: IteratorSignalProtocol[T] | None,
    timeout: float | None = None,
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[None]:
    """Submit an async generator to *loop* and drain it, propagating context.

    Like :func:`run_async_sharing_context` but for async generators: runs the
    generator to exhaustion on the loop, emitting each yielded value via
    *signals.next* before returning ``None``.  Cancellation and context
    propagation follow the same rules as :func:`run_async_sharing_context`.

    Args:
        coro: An async generator callable.
        loop: The koil event loop to run the generator on.
        signals: Optional signal holder for Qt integration callbacks.
        timeout: Maximum seconds the generator may take to drain *in total*,
            or ``None`` for no limit. Sits before ``*args``; callers forwarding
            user arguments positionally must pass it explicitly.
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        A :class:`KoilFuture` that resolves to ``None`` when the generator is
        exhausted.

    Raises:
        ValueError: If *timeout* is negative.
        ThreadCancelledError: If the current cancel event is already set when
            this function is called.
    """

    async def drain() -> None:
        __tracebackhide__ = True
        async for x in coro(*args, **kwargs):
            if signals:
                signals.next.emit((x, contextvars.copy_context()))
        return None

    return _submit(
        drain,
        loop,
        timeout,
        signals,
        signals.done if signals else None,
        "Generator",
        blocking=False,
    )


class _Step:
    """One ``next()``/``send()`` of a sync consumer, waiting on the generator's task."""

    __slots__ = (
        "send_value",
        "context",
        "future",
        "blocked_call",
        "reason",
        "timer",
        "grace_timer",
        "unwatch",
    )

    def __init__(
        self,
        send_value: Any,
        context: contextvars.Context,
        future: "concurrent.futures.Future[Any]",
        blocked_call: BlockedCall,
    ) -> None:
        self.send_value = send_value
        self.context = context
        self.future = future
        self.blocked_call = blocked_call
        self.reason: str | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.grace_timer: asyncio.TimerHandle | None = None
        self.unwatch: list[Callable[[], None]] = []


class AsyncGenDriver(Generic[T]):
    """Drives one async generator on the koil loop for a sync consumer.

    The generator lives in a single task for its whole life, and each
    ``next()`` of the consumer is handed to that task as a step. Resuming the
    generator from a fresh task per step would break everything a generator
    may hold across a ``yield`` that belongs to the task that entered it
    (``asyncio.timeout``, a ``TaskGroup``, an anyio cancel scope), and costs a
    task per item.

    Each step still sees the consumer's current ContextVars and publishes the
    generator's changes back, cancels like a single
    :func:`~koil.bridge.unkoil` call (which ends the generator), and is
    bounded by *timeout* when one is given.
    """

    def __init__(
        self,
        iterator: AsyncIterator[T],
        loop: asyncio.AbstractEventLoop,
        timeout: float | None = None,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._iterator = iterator
        self._loop = loop
        self._timeout = timeout
        self._grace = _cancel_grace(global_koil.get())
        self._context = contextvars.copy_context()
        self._steps: "asyncio.Queue[_Step | None]" = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._current: _Step | None = None
        self._last: KoilFuture[T] | None = None
        self._close_future: "concurrent.futures.Future[None] | None" = None
        self._close_requested = False
        self._close_error: BaseException | None = None

    # -- sync side ---------------------------------------------------------

    def step(self, send_value: Any = None) -> KoilFuture[T]:
        """Ask the generator for its next value; resolves like an ``unkoil`` call.

        The future raises :class:`StopAsyncIteration` when the generator is
        exhausted.
        """
        ambient = current_cancel_event.get()
        if ambient is not None and ambient.is_set():
            raise ThreadCancelledError("Thread was cancelled")

        baseline = contextvars.copy_context()
        context = contextvars.copy_context()
        blocked_call = BlockedCall(threading.get_ident())
        context.run(blocked_calls.set, blocked_calls.get() + (blocked_call,))

        future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
        own_cancel_event = KoilThreadSafeEvent(self._loop)
        step = _Step(send_value, context, future, blocked_call)

        _track(self._loop, future)
        self._loop.call_soon_threadsafe(self._begin, step)
        step.unwatch.append(
            _watch(own_cancel_event, self._loop, self._request, step, "cancel")
        )
        if ambient is not None:
            step.unwatch.append(
                _watch(ambient, self._loop, self._request, step, "ambient")
            )
        if future.done():
            for unwatch in step.unwatch:
                unwatch()

        self._last = KoilFuture(future, own_cancel_event, baseline, blocked_call)
        return self._last

    def close(self) -> None:
        """Close the generator on the loop so its ``finally`` blocks run now.

        Not whenever it is garbage-collected or the loop is torn down. A step
        still in flight (the consumer was interrupted, or timed out) is
        cancelled first and given a moment to unwind. An error the generator
        raises while closing is raised here.
        """
        last = self._last
        if last is not None and not last.future.done():
            last.cancel(reason="generator closed")
            _waiter(last.future).wait(CANCEL_GRACE_FALLBACK)

        if self._loop.is_closed():
            return

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            # Finalized on the loop itself (garbage collection): we cannot
            # block on the loop from inside it, so close without waiting.
            self._begin_close(None)
            return

        close_future: "concurrent.futures.Future[None]" = concurrent.futures.Future()
        try:
            _track(self._loop, close_future)
            self._loop.call_soon_threadsafe(self._begin_close, close_future)
        except RuntimeError:
            # Loop stopped between the check and the submission.
            return

        if not _waiter(close_future).wait(ACLOSE_TIMEOUT):
            logger.warning(
                "Timed out after %.1fs closing async generator %r on the koil loop; "
                "it may be wedged on uncancellable work.",
                ACLOSE_TIMEOUT,
                self._iterator,
            )
            return
        try:
            close_future.result()
        except concurrent.futures.CancelledError:
            pass  # the loop shut down, which closes its generators itself

    # -- loop side ---------------------------------------------------------

    def _ensure_task(self) -> "asyncio.Task[None]":
        if self._task is None:
            self._task = self._context.run(self._loop.create_task, self._drive())
            self._task.add_done_callback(self._on_driver_done)
        return self._task

    def _begin(self, step: _Step) -> None:
        task = self._ensure_task()
        if task.done():
            # The generator already ended (exhausted, raised, or cancelled).
            self._finish(step, error=StopAsyncIteration())
            return
        self._current = step
        step.blocked_call.task = task
        if self._timeout is not None:
            step.timer = self._loop.call_later(
                self._timeout, self._request, step, "timeout"
            )
        self._steps.put_nowait(step)

    def _request(self, step: _Step, reason: str) -> None:
        task = self._task
        if (
            task is None
            or task.done()
            or self._current is not step
            or step.future.done()
            or step.reason is not None
        ):
            return
        step.reason = reason
        task.cancel()
        if reason == "timeout":
            step.grace_timer = self._loop.call_later(self._grace, self._abandon, step)

    def _abandon(self, step: _Step) -> None:
        self._finish(
            step,
            error=_reason_error("timeout", self._timeout, "Task", acknowledged=False),
        )

    def _finish(
        self, step: _Step, *, value: Any = _MISSING, error: BaseException | None = None
    ) -> None:
        for handle in (step.timer, step.grace_timer):
            if handle is not None:
                handle.cancel()
        for unwatch in step.unwatch:
            unwatch()
        step.blocked_call.active = False
        if self._current is step:
            self._current = None
        _settle(step.future, value=value, error=error)

    async def _drive(self) -> None:
        __tracebackhide__ = True
        try:
            while True:
                step = await self._steps.get()
                if step is None:
                    return
                # The consumer may have changed its ContextVars since the
                # last step; the generator sees them as it would in sync code.
                _apply_context(step.context)
                try:
                    if step.send_value is None:
                        value = await self._iterator.__anext__()
                    else:
                        value = await self._iterator.asend(step.send_value)  # type: ignore[attr-defined]
                except asyncio.CancelledError:
                    raise
                except BaseException as e:
                    # StopAsyncIteration included: the generator is over.
                    self._finish(step, error=e)
                    return
                if step.reason is not None:
                    # Told to stop while it was producing this value.
                    return
                self._finish(step, value=(value, contextvars.copy_context()))
        finally:
            aclose = getattr(self._iterator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except asyncio.CancelledError:
                    raise
                except BaseException as e:
                    self._close_error = e

    def _on_driver_done(self, task: "asyncio.Task[None]") -> None:
        failure = None if task.cancelled() else task.exception()
        step = self._current
        if step is not None:
            error = failure
            if error is None or step.reason is not None:
                error = _reason_error(step.reason, self._timeout, "Task")
            self._finish(step, error=error)
        while not self._steps.empty():
            queued = self._steps.get_nowait()
            if queued is not None:
                self._finish(queued, error=StopAsyncIteration())
        if self._close_requested:
            self._resolve_close()

    def _begin_close(self, close_future: "concurrent.futures.Future[None] | None") -> None:
        self._close_requested = True
        self._close_future = close_future
        task = self._ensure_task()
        if task.done():
            self._resolve_close()
        else:
            self._steps.put_nowait(None)

    def _resolve_close(self) -> None:
        error, self._close_error = self._close_error, None
        if self._close_future is None:
            if error is not None:
                logger.warning(
                    "Closing async generator %r failed", self._iterator, exc_info=error
                )
            return
        if error is not None:
            _settle(self._close_future, error=error)
        else:
            _settle(self._close_future, value=None)
