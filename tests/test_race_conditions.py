"""Race-condition / concurrency regression tests for koil.

These tests deliberately hammer the thread <-> event-loop boundary to surface
timing bugs:

* the per-call helper-thread leak in the cancel bridge (``await_thread_event``),
* the shutdown race in ``Koil.__exit__`` (returning before the loop is closed),
* the process-global event-loop-policy race during concurrent ``Koil`` entry,
* contextvar leakage into pooled executor threads (``run_threaded``),
* check-then-act races when cancelling / completing a ``KoilFuture``.

They are intentionally light on assertions about *internal* timing and instead
assert the observable invariants (no leaked threads, loop actually closed,
correct results, no spurious exceptions) so they stay robust on slow CI.
"""

import asyncio
import concurrent.futures
import threading
import time
from typing import Callable

import pytest

from koil.bridge import run_threaded, unkoil, unkoil_gen, unkoil_task
from koil.loop import Koil, get_threaded_loop
from koil.context import current_cancel_event, global_koil_loop


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _threads_named(substr: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if substr in t.name]


def _wait_until(
    pred: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01
) -> bool:
    """Poll ``pred`` until it is truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


async def _quick() -> int:
    await asyncio.sleep(0)
    return 42


async def _quick_gen():
    for i in range(5):
        await asyncio.sleep(0)
        yield i


# --------------------------------------------------------------------------- #
# A1 - the cancel bridge must not spawn a helper thread per call
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(30)
def test_unkoil_does_not_spawn_helper_threads():
    """The cancel signal is now a thread-safe asyncio.Event awaited directly in
    the loop, so no bridging thread is created. Previously ``unkoil`` spawned a
    daemon thread per call and ``unkoil_gen`` spawned one *per yielded item*;
    asserting a flat total thread count is the strongest guard for the new
    mechanism."""
    with Koil():
        base = threading.active_count()
        peak = base
        for _ in range(50):
            assert unkoil(_quick) == 42
            peak = max(peak, threading.active_count())
        # unkoil_gen previously spawned one daemon thread for every yield.
        for _ in range(10):
            assert list(unkoil_gen(_quick_gen)) == [0, 1, 2, 3, 4]
            peak = max(peak, threading.active_count())

        # Peak (not just post-settle) thread count must stay at the baseline:
        # the old bridge spawned a short-lived daemon per call/item, which would
        # push the peak well above base even though they later drained.
        assert peak <= base, (
            f"helper threads were spawned: peak active_count {peak} > base {base}"
        )
        # The bridging thread mechanism is gone entirely.
        assert not _threads_named("wait_in_thread")


# --------------------------------------------------------------------------- #
# A2 - shutdown must join the loop thread and actually close the loop
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(15)
def test_exit_closes_loop_and_joins_thread():
    k = Koil()
    k.__enter__()
    loop = k._loop
    thread = k._loop_thread
    assert loop is not None and thread is not None

    assert unkoil(_quick) == 42

    k.__exit__(None, None, None)

    # The whole point of joining (vs. polling is_running()) is that we only
    # return once the loop is genuinely closed and the thread has exited.
    assert loop.is_closed(), "loop not closed after __exit__"
    assert not thread.is_alive(), "loop thread still alive after __exit__"
    assert k._loop is None and k._loop_thread is None


@pytest.mark.timeout(15)
def test_double_exit_is_safe():
    """A second __exit__ must not call stop() on an already-closed loop."""
    k = Koil()
    k.__enter__()
    k.__exit__(None, None, None)
    # Must not raise (loop reference is dropped on first exit).
    k.__exit__(None, None, None)


@pytest.mark.timeout(30)
def test_repeated_cycles_do_not_accumulate_loop_threads():
    # Guards that each __exit__ winds its loop thread down before the next
    # cycle. (The pre-fix busy-wait usually achieved this too; this is a
    # robustness guard for the join-based shutdown, not a strict A2 regression.)
    base = len(_threads_named("KoiledLoop"))
    for _ in range(15):
        with Koil():
            assert unkoil(_quick) == 42
        # Each cycle joins its loop thread synchronously on exit.
    assert _wait_until(
        lambda: len(_threads_named("KoiledLoop")) <= base, timeout=10.0
    ), (
        "KoiledLoop threads accumulated across enter/exit cycles: "
        f"{len(_threads_named('KoiledLoop'))} (base {base})"
    )


# --------------------------------------------------------------------------- #
# A3 - concurrent Koil entry must not race on the global event-loop policy
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(30)
def test_concurrent_koil_entry_is_safe():
    # Concurrency smoke test: 12 threads racing through Koil setup/teardown.
    # The policy assertion is a forward-looking invariant - the actual A3
    # regression (transient global-policy mutation) only manifests with
    # uvloop/Windows; see test_get_threaded_loop_never_mutates_global_policy
    # for the deterministic guard.
    policy_before = asyncio.get_event_loop_policy()
    errors: list[BaseException] = []
    barrier = threading.Barrier(12)

    def worker():
        try:
            barrier.wait()  # maximise overlap of the loop-creation window
            with Koil():
                for _ in range(20):
                    assert unkoil(_quick) == 42
        except BaseException as e:  # noqa: BLE001 - we want to see anything
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent Koil entry raised: {errors!r}"
    # The policy is a process-global; constructing loops directly must leave it
    # untouched no matter how many threads enter at once.
    assert asyncio.get_event_loop_policy() is policy_before


@pytest.mark.timeout(15)
def test_get_threaded_loop_never_mutates_global_policy(monkeypatch):
    """Loop creation must construct the desired loop directly, never via the
    process-global ``asyncio.set_event_loop_policy()`` (which races across
    concurrent ``Koil`` entries).

    This forces the uvloop branch with a fake module - independent of whether
    uvloop is actually installed - and spies on the global policy setter. The
    pre-fix implementation set+restored the policy here (2 calls); the fixed
    implementation must make zero policy mutations.
    """
    import koil.loop as koil_mod

    class _FakeUvloop:
        EventLoopPolicy = asyncio.DefaultEventLoopPolicy
        new_event_loop = staticmethod(asyncio.new_event_loop)

    monkeypatch.setattr(koil_mod, "uvloop", _FakeUvloop)

    policy_calls: list[object] = []
    real_set = asyncio.set_event_loop_policy

    def spy(policy):
        policy_calls.append(policy)
        return real_set(policy)

    monkeypatch.setattr(asyncio, "set_event_loop_policy", spy)

    result = get_threaded_loop("policy-spy", uvify=True)
    # Tolerate either return shape so the policy assertion is reached on both
    # the fixed (loop, thread) and the pre-fix (loop) implementations.
    loop = result[0] if isinstance(result, tuple) else result
    thread = result[1] if isinstance(result, tuple) else None
    try:
        assert policy_calls == [], (
            f"global event loop policy was mutated during loop creation: {policy_calls!r}"
        )
        fut = asyncio.run_coroutine_threadsafe(_quick(), loop)
        assert fut.result(timeout=5) == 42
    finally:
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# A4 - run_threaded must not leak contextvars into pooled executor threads
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(15)
async def test_run_spawned_does_not_leak_contextvars_to_pool_thread():
    """Pin the loop to a single executor worker so the thread is guaranteed to
    be reused, then prove koil's contextvars do not survive into an unrelated
    job on that same thread."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="koil-race-pool"
        )
    )

    def koiled_body() -> int:
        # Inside the spawned task koil's contextvars are populated...
        assert current_cancel_event.get() is not None
        return threading.get_ident()

    ident_inside = await run_threaded(koiled_body)

    def probe():
        # ...but a plain executor job on the *same* worker thread must see a
        # clean context (no leaked cancel event / loop).
        return (
            threading.get_ident(),
            current_cancel_event.get(),
            global_koil_loop.get(),
        )

    ident_probe, leaked_cancel, leaked_loop = await loop.run_in_executor(None, probe)

    assert ident_probe == ident_inside, "test precondition: worker not reused"
    assert leaked_cancel is None, "cancel event leaked into pooled thread"
    assert leaked_loop is None, "koil loop leaked into pooled thread"


@pytest.mark.timeout(15)
async def test_concurrent_run_spawned_have_isolated_cancel_events():
    """Concurrency smoke test: many run_threaded tasks each see their own cancel
    event - one task setting its event must not be observed by another. (Holds
    with or without the A4 fix, since each wrapper installs its own event; this
    guards against future regressions in cancel-event scoping.)"""
    started = threading.Barrier(5)
    observed: list[bool] = []
    lock = threading.Lock()

    def body(self_cancel: bool):
        started.wait()
        ev = current_cancel_event.get()
        assert ev is not None
        if self_cancel:
            ev.set()
        time.sleep(0.02)
        # Each task must only ever see *its own* cancellation state.
        with lock:
            observed.append(ev.is_set())
        return self_cancel

    results = await asyncio.gather(
        run_threaded(body, True),
        run_threaded(body, False),
        run_threaded(body, False),
        run_threaded(body, False),
        run_threaded(body, False),
    )
    assert results == [True, False, False, False, False]
    # Exactly one task set its event; nobody else should have observed a set.
    assert observed.count(True) == 1


# --------------------------------------------------------------------------- #
# concurrency through a single shared koil loop
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(30)
def test_concurrent_unkoil_into_shared_loop():
    """Many OS threads submitting coroutines into one koil loop concurrently
    must all get correct, non-cross-contaminated results."""
    with Koil():
        loop = global_koil_loop.get()
        assert loop is not None

        n = 40
        results: dict[int, int] = {}
        errors: list[BaseException] = []
        barrier = threading.Barrier(n)

        def worker(i: int):
            # Threads start with a fresh context, so propagate the loop the way
            # koil's own spawn wrappers do.
            global_koil_loop.set(loop)

            async def coro() -> int:
                await asyncio.sleep(0.001)
                return i * 2

            try:
                barrier.wait()
                results[i] = unkoil(coro)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, f"concurrent unkoil raised: {errors!r}"
        assert results == {i: i * 2 for i in range(n)}


# --------------------------------------------------------------------------- #
# cancel-vs-complete race on a KoilFuture
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(30)
def test_cancel_completion_race_never_corrupts_result():
    """Cancel a task right as it is about to complete, many times. The result
    must be either the value or a CancelledError - never a hang, an
    InvalidStateError, or a wrong value."""
    with Koil():
        base = threading.active_count()
        for _ in range(60):

            async def coro() -> int:
                await asyncio.sleep(0)  # resolves almost immediately
                return 7

            fut = unkoil_task(coro)
            fut.cancel()  # races against completion
            try:
                assert fut.result() == 7
            except concurrent.futures.CancelledError:
                pass  # cancellation won the race - acceptable

        # The cancel path must not spawn/leak threads either.
        assert _wait_until(lambda: threading.active_count() <= base, timeout=10.0)


@pytest.mark.timeout(15)
def test_koil_future_cancel_is_prompt():
    """Cancelling a long-running KoilFuture must resolve fast (via the cancel
    event), not block until the coroutine would have finished."""
    with Koil():

        async def slow() -> str:
            await asyncio.sleep(30)
            return "done"

        fut = unkoil_task(slow)
        time.sleep(0.05)  # let the coroutine actually start

        start = time.monotonic()
        assert fut.cancel() is True
        with pytest.raises(concurrent.futures.CancelledError):
            fut.result()
        elapsed = time.monotonic() - start
        assert elapsed < 5, f"cancellation took {elapsed:.2f}s (should be sub-second)"


# --------------------------------------------------------------------------- #
# nested cancellation: unkoil() inside a run_threaded worker
# --------------------------------------------------------------------------- #
@pytest.mark.timeout(15)
async def test_nested_unkoil_in_run_spawned_cancels_promptly():
    """unkoil() called from inside a run_threaded worker shares the worker's
    cancel event. Cancelling the outer task must promptly cancel the inner
    coroutine (it inherits and awaits the *same* thread-safe asyncio.Event), not
    block until the inner would have finished on its own.

    This is the path that would silently break if the spawn helpers kept a
    plain (non-awaitable) threading.Event.
    """
    inner_started = threading.Event()

    def sync_body() -> str:
        async def inner() -> str:
            inner_started.set()
            await asyncio.sleep(30)  # only cancellation should end this
            return "inner-done"

        return unkoil(inner)

    async def outer() -> str:
        return await run_threaded(sync_body)

    task = asyncio.create_task(outer())

    # Wait until the nested inner coroutine is actually running.
    await asyncio.get_running_loop().run_in_executor(None, inner_started.wait)
    await asyncio.sleep(0.05)

    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"nested cancellation took {elapsed:.2f}s (should be prompt)"

    # No bridging threads were used to achieve this. (We do not assert a flat
    # active_count here: run_threaded uses run_in_executor, whose pool keeps idle
    # worker threads alive by design - that is not a leak.)
    assert not _threads_named("wait_in_thread")


# --------------------------------------------------------------------------- #
# B1 (Qt) - guarded future setters are race-safe. Runs only with PyQt5/CI.
# --------------------------------------------------------------------------- #
def test_qt_guarded_setters_ignore_double_resolution():
    pytest.importorskip("PyQt5")
    from koil.qt import _set_future_result, _set_future_exception

    async def check():
        loop = asyncio.get_running_loop()

        f = loop.create_future()
        _set_future_result(f, "a")
        # Second resolution and a competing rejection must be ignored, not raise
        # InvalidStateError.
        _set_future_result(f, "b")
        _set_future_exception(f, ValueError("late"))
        assert f.result() == "a"

        g = loop.create_future()
        _set_future_exception(g, ValueError("boom"))
        _set_future_result(g, "ignored")
        with pytest.raises(ValueError, match="boom"):
            g.result()

    asyncio.run(check())
