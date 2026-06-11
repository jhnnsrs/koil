"""Internal utilities: introspection helpers, future wrappers, and context runners.

The two central types here are :class:`KoilFuture` and :class:`KoilIterator`,
which wrap a :class:`concurrent.futures.Future` with a koil-aware cancel
mechanism.  :func:`run_async_sharing_context` and
:func:`iterate_async_sharing_context` are the low-level primitives that submit
coroutines/generators to the koil loop while propagating the caller's
:class:`~contextvars.Context` and cancellation event.
"""
import asyncio
import contextvars
import concurrent.futures
import time
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    ParamSpec,
    Tuple,
    TypeVar,
    cast,
)
from koil.errors import CancelledError, KoilTimeoutError, ThreadCancelledError
from koil.types import AnyCallable
from koil.context import current_cancel_event, global_koil, KoilThreadSafeEvent
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

#: Default interval, in seconds, at which a blocking
#: :meth:`KoilFuture.result` wakes up to let the interpreter deliver a pending
#: signal (e.g. a Ctrl+C SIGINT).
#:
#: Python only runs signal handlers on the main thread when it returns from a
#: C-level blocking call, so an indefinite ``Future.result()`` would defer a
#: KeyboardInterrupt until the task completes. This poll interval bounds that
#: delay. It adds *no* latency to fast tasks: ``result(timeout=...)`` returns the
#: instant the future is done, so the timeout only ever fires while genuinely
#: waiting.
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


def _safe_set_event(event: asyncio.Event) -> None:
    """Set *event*, tolerating a closed event loop.

    :class:`~koil.context.KoilThreadSafeEvent.set` routes through
    ``call_soon_threadsafe``, which raises :class:`RuntimeError` when the koil
    loop is already closed. If the loop is gone the work is already
    finished/cancelled, so swallowing that error is correct.
    """
    try:
        event.set()
    except RuntimeError:
        pass


class _KoilFutureBase(Generic[T]):
    """Shared implementation for :class:`KoilFuture` and :class:`KoilIterator`.

    Wraps a :class:`concurrent.futures.Future` with a koil-aware cancel
    mechanism: cancellation is signalled via a
    :class:`~koil.context.KoilThreadSafeEvent` rather than by attempting to
    interrupt the underlying thread directly.
    """

    def __init__(
        self,
        future: concurrent.futures.Future[Tuple[T, contextvars.Context]],
        cancel_event: asyncio.Event,
    ) -> None:
        super().__init__()
        self.future = future
        self.cancel_event = cancel_event
        self._cancel_requested = False
        self._cancel_reason: str | None = None
        self._timed_out = False

    def cancel(self, reason: str | None = None) -> bool:
        """Request cancellation of the running task.

        Sets the associated cancel event so that koil worker code that checks
        :func:`~koil.context.check_cancelled` will raise
        :class:`~koil.errors.ThreadCancelledError` on its next poll.

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
        done, _ = concurrent.futures.wait([self.future], timeout=grace)
        if not done:
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

        Also propagates any updated :class:`~contextvars.ContextVar` values
        from the worker back into the calling context.

        The wait is performed in short slices rather than as one indefinite
        block so that a Ctrl+C (SIGINT) on the main thread is delivered promptly
        instead of being swallowed until the coroutine finishes on the
        background loop.

        Args:
            timeout: Maximum seconds to wait for the result. On expiry the task
                is signalled to cancel (cooperatively, with the ambient
                :attr:`~koil.loop.Koil.cancel_timeout` as acknowledgement
                grace) and :class:`~koil.errors.KoilTimeoutError` is raised.
                ``None`` (the default) waits indefinitely. ``0`` requires the
                task to already be done.
            poll_interval: Seconds to wait per slice before re-checking for a
                pending signal. Defaults to the module-wide
                :data:`RESULT_POLL_INTERVAL`.

        Raises:
            KoilTimeoutError: If *timeout* elapses before the task completes.
            ValueError: If *timeout* is negative.
            CancelledError: If the task was cancelled.
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
        interval = RESULT_POLL_INTERVAL if poll_interval is None else poll_interval
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            # Wait in bounded slices so a pending Ctrl+C on the main thread is
            # delivered between slices instead of deferred until the task
            # completes. Use the non-raising concurrent.futures.wait() rather
            # than result(timeout=...): on 3.11 concurrent.futures.TimeoutError
            # *is* the builtin TimeoutError, so catching a result() timeout would
            # also swallow a real TimeoutError raised by the coroutine and spin
            # forever. wait() never raises, so the subsequent result() re-raises
            # the coroutine's actual exception (TimeoutError included).
            # A done() check always precedes the deadline check, so a completed
            # future returns its result even with timeout=0.
            while not self.future.done():
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._timeout_expired(timeout)  # type: ignore[arg-type]
                    slice_timeout = min(interval, remaining)
                else:
                    slice_timeout = interval
                concurrent.futures.wait([self.future], timeout=slice_timeout)
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

        for ctx, value in context.items():
            ctx.set(value)

        return res


class KoilFuture(_KoilFutureBase[T]):
    """A cancellable future representing a single coroutine submitted to the koil loop.

    Returned by :func:`~koil.bridge.unkoil_task` and used internally by
    :func:`~koil.utils.run_async_sharing_context`. Cancellation is cooperative:
    calling :meth:`cancel` sets a :class:`~koil.context.KoilThreadSafeEvent`
    that the running coroutine (or its worker wrapper) checks periodically.
    """


class KoilIterator(_KoilFutureBase[T]):
    """A cancellable future representing an async iterator submitted to the koil loop.

    Returned by :func:`~koil.utils.iterate_async_sharing_context`. Shares the
    same cancellation semantics as :class:`KoilFuture`.
    """


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

    * Copies the caller's :class:`~contextvars.Context` into the coroutine so
      that :class:`~contextvars.ContextVar` reads inside the coroutine see the
      values set by the calling thread.
    * Races the coroutine against the active cancel event; if the event fires
      first, the coroutine is cancelled and
      :class:`~asyncio.CancelledError` is raised.
    * Optionally races the coroutine against a *timeout* enforced on the loop;
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
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")

    ctxs = contextvars.copy_context()
    # Captured on the calling thread: the loop-side task context typically has
    # no ambient koil, but the cancellation grace below should follow the
    # caller's koil configuration.
    submitting_koil = global_koil.get()

    # Two distinct cancel events, deliberately *not* the same object:
    #
    # * ``own_cancel_event`` is private to this single future. It is what
    #   :meth:`KoilFuture.cancel` (and the Ctrl+C path in
    #   :meth:`KoilFuture.result`) sets, so cancelling *this* future cancels
    #   only this future.
    # * ``ambient_cancel_event`` is the scope-wide event injected by
    #   :func:`~koil.bridge.run_threaded` into ``current_cancel_event``. We race
    #   against it (read-only) so that tearing down the enclosing worker still
    #   cancels nested work — but we never *set* it here.
    #
    # Previously both roles were collapsed onto a single shared event stored on
    # the future. Cancelling one future (or a Ctrl+C consumed by ``result()``)
    # then set the ambient event, poisoning every later ``unkoil()`` that shared
    # it (e.g. a context-manager teardown after the interrupt) with a spurious
    # ``ThreadCancelledError``.
    ambient_cancel_event = current_cancel_event.get()
    own_cancel_event = KoilThreadSafeEvent(loop)

    if ambient_cancel_event is not None and ambient_cancel_event.is_set():
        raise ThreadCancelledError("Thread was cancelled")

    async def passed_with_context():
        __tracebackhide__ = True

        async def context_future():
            __tracebackhide__ = True
            for ctx, value in ctxs.items():
                ctx.set(value)

            result = await coro(*args, **kwargs)

            newcontext = contextvars.copy_context()
            return result, newcontext

        future_t = asyncio.create_task(context_future())
        cancel_waiters = [asyncio.create_task(own_cancel_event.wait())]
        if ambient_cancel_event is not None:
            cancel_waiters.append(asyncio.create_task(ambient_cancel_event.wait()))
        timeout_waiter = (
            asyncio.create_task(asyncio.sleep(timeout)) if timeout is not None else None
        )

        race = [future_t, *cancel_waiters]
        if timeout_waiter is not None:
            race.append(timeout_waiter)

        finished, unfinished = await asyncio.wait(
            race, return_when=asyncio.FIRST_COMPLETED
        )

        # Tidy up whatever lost the race (a pending cancel/timeout waiter, or
        # the work task when a cancel event or the deadline fired first).
        # Cancelling ``future_t`` here is what unwinds an in-flight
        # coroutine/async-generator step. When the deadline fired, the wait for
        # the work task to acknowledge is bounded by the caller's
        # cancel_timeout grace so an uncancellable coroutine cannot wedge the
        # future forever.
        timed_out = timeout_waiter is not None and timeout_waiter in finished
        acknowledged = True
        for untask in unfinished:
            untask.cancel()
            try:
                if untask is future_t and timed_out:
                    grace = _cancel_grace(submitting_koil)
                    await asyncio.wait_for(asyncio.shield(untask), timeout=grace)
                else:
                    await untask
            except asyncio.TimeoutError:
                acknowledged = False
            except asyncio.CancelledError:
                pass

        if future_t in finished:
            exception = future_t.exception()
            if exception:
                # Prune on the loop side so signal handlers (the Qt errored
                # slots) receive a clean traceback too. global_koil is
                # typically unset in this task's context, so should_rewrite()
                # falls back to the module default. The sync-side boundary
                # prunes the same exception object again — idempotent.
                if should_rewrite():
                    prune_traceback(exception)

                if signals:
                    signals.errored.emit(exception)

                raise exception

            typed_task = cast(asyncio.Task[Tuple[T, contextvars.Context]], future_t)
            result = typed_task.result()

            if signals:
                try:
                    signals.returned.emit(result)
                except Exception as e:
                    raise e

            return result

        if timed_out:
            # The deadline won the race. A timeout is an error, not a
            # cancellation, so the errored signal fires and the cancelled
            # signal does not.
            if acknowledged:
                error: BaseException = KoilTimeoutError(
                    f"Task did not complete within {timeout}s; it was cancelled."
                )
            else:
                error = KoilTimeoutError(
                    f"Task did not complete within {timeout}s and did not "
                    f"acknowledge cancellation within the grace period. Make "
                    f"sure the task is not blocking the loop with "
                    f"uncancellable work and that workers call "
                    f"check_cancelled periodically."
                )

            if signals:
                signals.errored.emit(error)

            raise error

        # A cancel event won the race.
        error = asyncio.CancelledError("Future was cancelled")

        if signals:
            signals.cancelled.emit(error)

        raise error

    return KoilFuture(
        asyncio.run_coroutine_threadsafe(passed_with_context(), loop), own_cancel_event
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
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")

    ctxs = contextvars.copy_context()
    submitting_koil = global_koil.get()

    # See run_async_sharing_context for why the per-future and ambient cancel
    # events are kept distinct: cancelling this future must not poison the
    # shared ambient scope event read by later unkoil() calls.
    ambient_cancel_event = current_cancel_event.get()
    own_cancel_event = KoilThreadSafeEvent(loop)

    if ambient_cancel_event is not None and ambient_cancel_event.is_set():
        raise ThreadCancelledError("Thread was cancelled")

    async def passed_with_context():
        __tracebackhide__ = True

        async def context_future():
            __tracebackhide__ = True
            for ctx, value in ctxs.items():
                ctx.set(value)

            async for x in coro(*args, **kwargs):
                newcontext = contextvars.copy_context()
                if signals:
                    signals.next.emit((x, newcontext))

            newcontext = contextvars.copy_context()
            return None, newcontext

        future_t = asyncio.create_task(context_future())
        cancel_waiters = [asyncio.create_task(own_cancel_event.wait())]
        if ambient_cancel_event is not None:
            cancel_waiters.append(asyncio.create_task(ambient_cancel_event.wait()))
        timeout_waiter = (
            asyncio.create_task(asyncio.sleep(timeout)) if timeout is not None else None
        )

        race = [future_t, *cancel_waiters]
        if timeout_waiter is not None:
            race.append(timeout_waiter)

        finished, unfinished = await asyncio.wait(
            race, return_when=asyncio.FIRST_COMPLETED
        )

        timed_out = timeout_waiter is not None and timeout_waiter in finished
        acknowledged = True
        for untask in unfinished:
            untask.cancel()
            try:
                if untask is future_t and timed_out:
                    grace = _cancel_grace(submitting_koil)
                    await asyncio.wait_for(asyncio.shield(untask), timeout=grace)
                else:
                    await untask
            except asyncio.TimeoutError:
                acknowledged = False
            except asyncio.CancelledError:
                pass

        if future_t in finished:
            exception = future_t.exception()
            if exception:
                # See run_async_sharing_context: prune before emitting so Qt
                # errored slots get a clean traceback; module default applies.
                if should_rewrite():
                    prune_traceback(exception)

                if signals:
                    signals.errored.emit(exception)

                raise exception

            typed_task = cast(asyncio.Task[Tuple[None, contextvars.Context]], future_t)
            result = typed_task.result()

            if signals:
                try:
                    signals.done.emit(None)
                except Exception as e:
                    raise e

            return result

        if timed_out:
            # See run_async_sharing_context: a timeout is an error, not a
            # cancellation.
            if acknowledged:
                timeout_error: BaseException = KoilTimeoutError(
                    f"Generator did not finish within {timeout}s; it was cancelled."
                )
            else:
                timeout_error = KoilTimeoutError(
                    f"Generator did not finish within {timeout}s and did not "
                    f"acknowledge cancellation within the grace period."
                )

            if signals:
                signals.errored.emit(timeout_error)

            raise timeout_error

        error = CancelledError("Future was cancelled")

        if signals:
            signals.cancelled.emit(error)

        raise error

    return KoilFuture(
        asyncio.run_coroutine_threadsafe(passed_with_context(), loop), own_cancel_event
    )
