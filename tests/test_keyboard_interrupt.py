"""Tests for keyboard-interrupt handling and bounded shutdown.

These cover the three behaviours added to make ``unkoil`` interruptible:

* :meth:`KoilFuture.result` waits in bounded slices so a Ctrl+C (SIGINT) on the
  main thread is delivered promptly instead of being deferred until the
  coroutine finishes — without swallowing a real ``TimeoutError`` raised by the
  coroutine (regression guard).
* The poll interval is configurable via the global
  :data:`koil.utils.RESULT_POLL_INTERVAL` and a per-call ``poll_interval`` arg.
* :meth:`KoilFuture.cancel` records an optional ``reason``.
* :meth:`Koil.__exit__` abandons a loop thread wedged on uncancellable work
  after a bounded grace instead of hanging forever.

The interrupt is simulated with :func:`_thread.interrupt_main`, which queues a
``KeyboardInterrupt`` on the main (test) thread exactly as a real Ctrl+C would.
Each test carries a tight ``@pytest.mark.timeout`` so a regression fails fast
instead of hanging the suite.
"""

import asyncio
import logging
import threading
import time
import _thread

import pytest

import koil.utils as ku
from koil import Koil
from koil.bridge import unkoil, unkoil_task


def _interrupt_main_after(delay: float) -> None:
    """Start a daemon thread that fires a KeyboardInterrupt after *delay*."""

    def fire() -> None:
        time.sleep(delay)
        _thread.interrupt_main()

    threading.Thread(target=fire, daemon=True).start()


async def _never() -> None:
    """A coroutine that blocks ~forever but is cancellable (has an await)."""
    await asyncio.sleep(30)


# --------------------------------------------------------------------------- #
# Regression: a real TimeoutError from the coroutine must propagate           #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_coroutine_timeout_error_propagates() -> None:
    """A bare ``TimeoutError`` raised by the coroutine propagates to the caller.

    On 3.11 ``concurrent.futures.TimeoutError`` *is* the builtin ``TimeoutError``;
    the poll loop must not mistake the coroutine's exception for a poll-slice
    timeout (which would busy-loop forever).
    """

    async def boom() -> None:
        raise TimeoutError("from the coroutine")

    with Koil():
        with pytest.raises(TimeoutError, match="from the coroutine"):
            unkoil(boom)


@pytest.mark.timeout(10)
def test_asyncio_wait_for_timeout_propagates() -> None:
    """A realistic ``asyncio.wait_for`` timeout propagates rather than hanging."""

    async def slow() -> None:
        await asyncio.sleep(10)

    async def with_timeout() -> None:
        await asyncio.wait_for(slow(), timeout=0.05)

    with Koil():
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            unkoil(with_timeout)


# --------------------------------------------------------------------------- #
# KeyboardInterrupt propagates out of a blocking unkoil/result                #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_keyboard_interrupt_propagates_from_unkoil() -> None:
    """Ctrl+C while blocked in ``unkoil`` raises ``KeyboardInterrupt`` promptly."""
    with Koil():
        _interrupt_main_after(0.3)
        with pytest.raises(KeyboardInterrupt):
            unkoil(_never)


@pytest.mark.timeout(10)
def test_keyboard_interrupt_sets_cancel_reason() -> None:
    """The interrupt path cooperatively cancels the future with a reason."""
    with Koil():
        fut = unkoil_task(_never)
        _interrupt_main_after(0.3)
        with pytest.raises(KeyboardInterrupt):
            fut.result()
        assert fut.cancelled()
        assert fut.cancel_reason == "keyboard interrupt"


# --------------------------------------------------------------------------- #
# cancel(reason=...) bookkeeping                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_cancel_records_reason() -> None:
    """``cancel(reason=...)`` is exposed via ``cancel_reason``; absent by default."""
    with Koil():
        fut = unkoil_task(_never)
        assert fut.cancel_reason is None
        assert fut.cancel(reason="manual stop") is True
        assert fut.cancel_reason == "manual stop"
        assert fut.cancelled()

        without_reason = unkoil_task(_never)
        assert without_reason.cancel() is True
        assert without_reason.cancel_reason is None


# --------------------------------------------------------------------------- #
# Poll interval is configurable (per-call override and global default)        #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_per_call_poll_interval_is_honored() -> None:
    """A larger ``poll_interval`` defers interrupt delivery to the next slice."""
    with Koil():
        fut = unkoil_task(_never)
        _interrupt_main_after(0.05)
        t0 = time.time()
        with pytest.raises(KeyboardInterrupt):
            fut.result(poll_interval=0.4)
        dt = time.time() - t0
        # Interrupt fired at ~0.05s but is only delivered when the 0.4s wait
        # slice returns; with the default 0.05s slice this would be ~0.05s.
        assert dt >= 0.3, f"poll_interval not honored, dt={dt:.2f}s"


@pytest.mark.timeout(10)
def test_global_poll_interval_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reassigning the module-global default takes effect at call time."""
    monkeypatch.setattr(ku, "RESULT_POLL_INTERVAL", 0.4)
    with Koil():
        fut = unkoil_task(_never)
        _interrupt_main_after(0.05)
        t0 = time.time()
        with pytest.raises(KeyboardInterrupt):
            fut.result()
        dt = time.time() - t0
        assert dt >= 0.3, f"global RESULT_POLL_INTERVAL not honored, dt={dt:.2f}s"


# --------------------------------------------------------------------------- #
# Bounded shutdown: a wedged loop thread is abandoned, not joined forever      #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_exit_abandons_wedged_loop_thread(caplog: pytest.LogCaptureFixture) -> None:
    """``__exit__`` returns within the bounded grace even if the loop is wedged."""

    async def wedge() -> None:
        # A blocking call with no await: the event loop thread cannot run
        # call_soon_threadsafe(stop), so a naive unbounded join would hang.
        time.sleep(2.0)

    k = Koil(shutdown_join_timeout=0.1)
    k.cancel_timeout = 0.1
    k.__enter__()
    unkoil_task(wedge)
    time.sleep(0.15)  # let the loop pick up and begin running the wedging coro

    caplog.set_level(logging.WARNING)
    t0 = time.time()
    k.__exit__(None, None, None)
    dt = time.time() - t0

    assert dt < 1.5, f"__exit__ hung ({dt:.2f}s); wedged thread was not abandoned"
    assert "abandon" in caplog.text.lower(), "expected an abandonment warning"
    # The daemon thread that's still sleeping self-releases shortly after; it
    # cannot block interpreter exit.


@pytest.mark.timeout(10)
def test_clean_exit_is_prompt() -> None:
    """A clean shutdown does not wait out the timeouts."""

    async def quick() -> int:
        return 1

    t0 = time.time()
    with Koil() as k:
        # Large timeouts would dominate if exit accidentally always waited them.
        k.cancel_timeout = 5.0
        k.shutdown_join_timeout = 5.0
        assert unkoil(quick) == 1
    dt = time.time() - t0
    assert dt < 1.0, f"clean exit was delayed ({dt:.2f}s)"
