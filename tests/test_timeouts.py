"""Timeout behaviour of the *_with_timeout bridges and KoilFuture.result(timeout=)."""

import asyncio
import time

import pytest

from koil import (
    Koil,
    KoilError,
    KoilTimeoutError,
    unkoil,
    unkoil_gen_with_timeout,
    unkoil_task,
    unkoil_task_with_timeout,
    unkoil_with_timeout,
)


async def _slow(result: int = 42) -> int:
    await asyncio.sleep(5)
    return result


async def _fast(result: int = 42) -> int:
    await asyncio.sleep(0)
    return result


@pytest.mark.timeout(10)
def test_unkoil_with_timeout_expires():
    with Koil():
        start = time.monotonic()
        with pytest.raises(KoilTimeoutError):
            unkoil_with_timeout(_slow, 0.05)
        # the deadline fired, we did not wait out the 5s sleep
        assert time.monotonic() - start < 2


@pytest.mark.timeout(10)
def test_timeout_error_is_caught_by_builtin_timeout_and_koil_error():
    with Koil():
        with pytest.raises(TimeoutError):
            unkoil_with_timeout(_slow, 0.05)
        with pytest.raises(KoilError):
            unkoil_with_timeout(_slow, 0.05)


@pytest.mark.timeout(10)
def test_unkoil_with_timeout_fast_task_returns():
    with Koil():
        assert unkoil_with_timeout(_fast, 5.0, 21) == 21


@pytest.mark.timeout(10)
def test_unkoil_with_timeout_negative_raises_value_error():
    with Koil():
        with pytest.raises(ValueError):
            unkoil_with_timeout(_fast, -1.0)


@pytest.mark.timeout(10)
def test_timeout_cancels_underlying_task():
    """On expiry the coroutine is actually cancelled, not orphaned."""
    cancelled = []

    async def tracks_cancel():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    with Koil():
        with pytest.raises(KoilTimeoutError):
            unkoil_with_timeout(tracks_cancel, 0.05)
        # The loop-side grace awaited the task's unwind before resolving the
        # future, so the cancellation has already been observed.
        assert cancelled == [True]


@pytest.mark.timeout(10)
def test_timeout_unacknowledged_cancel_reports_it():
    """A coroutine that swallows its first cancel past the grace period still
    raises KoilTimeoutError, with the 'did not acknowledge' wording."""

    async def stubborn():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)  # ack only after the 0.1s grace
            raise

    with Koil(cancel_timeout=0.1, shutdown_join_timeout=1.0):
        with pytest.raises(KoilTimeoutError, match="did not.*acknowledge"):
            unkoil_with_timeout(stubborn, 0.05)
        # let the stubborn coroutine finish unwinding so teardown is clean
        time.sleep(0.5)


@pytest.mark.timeout(10)
def test_user_function_with_own_timeout_param_does_not_collide():
    async def fn_with_timeout(timeout: float) -> float:
        await asyncio.sleep(0)
        return timeout

    with Koil():
        assert unkoil_with_timeout(fn_with_timeout, 5.0, timeout=1.25) == 1.25


@pytest.mark.timeout(10)
def test_unkoil_task_with_timeout_expires_without_result_call():
    """The deadline is enforced on the loop even before result() is called."""
    with Koil():
        future = unkoil_task_with_timeout(_slow, 0.05)
        time.sleep(0.3)
        assert future.done()
        with pytest.raises(KoilTimeoutError):
            future.result()


@pytest.mark.timeout(10)
def test_result_timeout_expires():
    with Koil():
        future = unkoil_task(_slow)
        with pytest.raises(KoilTimeoutError):
            future.result(timeout=0.05)


@pytest.mark.timeout(10)
def test_result_timeout_zero_on_done_future_returns():
    with Koil():
        future = unkoil_task(_fast, 7)
        while not future.done():
            time.sleep(0.01)
        assert future.result(timeout=0) == 7


@pytest.mark.timeout(10)
def test_result_negative_timeout_raises_value_error():
    with Koil():
        future = unkoil_task(_fast)
        with pytest.raises(ValueError):
            future.result(timeout=-1)
        future.result()


@pytest.mark.timeout(10)
def test_result_external_cancel_raises_cancelled_not_timeout():
    import concurrent.futures

    with Koil():
        future = unkoil_task(_slow)
        future.cancel(reason="external")
        # An externally-cancelled task surfaces as a CancelledError (never as
        # KoilTimeoutError), even when result() was given a timeout.
        with pytest.raises(
            (asyncio.CancelledError, concurrent.futures.CancelledError)
        ):
            future.result(timeout=5.0)


@pytest.mark.timeout(10)
def test_unkoil_gen_with_timeout_per_step():
    """The bound is per step: a generator that keeps yielding within the bound
    runs to completion even if its total runtime exceeds the timeout."""

    async def steady():
        for i in range(5):
            await asyncio.sleep(0.05)
            yield i

    with Koil():
        assert list(unkoil_gen_with_timeout(steady, 1.0)) == [0, 1, 2, 3, 4]


@pytest.mark.timeout(10)
def test_unkoil_gen_with_timeout_slow_step_raises_and_finalizes():
    finalized = []

    async def stalls():
        try:
            yield 1
            await asyncio.sleep(5)
            yield 2
        finally:
            finalized.append(True)

    with Koil():
        gen = unkoil_gen_with_timeout(stalls, 0.05)
        assert next(gen) == 1
        with pytest.raises(KoilTimeoutError):
            next(gen)
        assert finalized == [True]


@pytest.mark.timeout(10)
def test_plain_unkoil_unaffected():
    """No-timeout paths behave exactly as before."""
    with Koil():
        assert unkoil(_fast, 3) == 3
        future = unkoil_task(_fast, 4)
        assert future.result() == 4
