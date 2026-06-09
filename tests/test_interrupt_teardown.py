"""Tests for clean teardown after a cancellation / Ctrl+C.

Two distinct bugs are covered here:

#1 *Cancel-event poisoning.* ``KoilFuture.cancel()`` used to set the *shared*
   ambient cancel event (the one injected by :func:`~koil.bridge.run_threaded`
   into ``current_cancel_event``), because the future stored that very event.
   Cancelling one future — or a Ctrl+C consumed by :meth:`KoilFuture.result` —
   therefore poisoned every *later* ``unkoil()`` sharing that ambient event with
   a spurious :class:`~koil.errors.ThreadCancelledError`, so a context-manager
   teardown after an interrupt could never run. See
   ``test_cancel_does_not_poison_subsequent_unkoil_in_worker``.

#2 *Double-close async-generator race.* Tearing a follow-style async generator
   down from two paths at once (a cancelled ``__anext__`` still unwinding, plus
   an explicit ``aclose()``) raises
   ``RuntimeError("aclose(): asynchronous generator is already running")``.
   :func:`~koil.utils.aclose_async_gen_threadsafe` makes that close idempotent.
   See ``test_double_close_of_async_gen_is_safe``.

The poisoning only manifests where an *ambient* cancel event exists — i.e.
inside a :func:`~koil.bridge.run_threaded` worker. On the bare main thread every
``unkoil`` gets a fresh per-call event, so the plan's literal main-thread Test A
is a no-op that passes regardless; the honest reproduction nests the calls in a
worker (see the docstring on that test).
"""

import asyncio
import concurrent.futures
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from koil import Koil
from koil.bridge import get_koiled_loop_or_raise, run_threaded, unkoil, unkoil_task
from koil.errors import ThreadCancelledError
from koil.utils import aclose_async_gen_threadsafe, run_async_sharing_context


# --------------------------------------------------------------------------- #
# Bug #1 — a consumed cancellation must not poison later unkoil() calls        #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_cancel_does_not_poison_subsequent_unkoil_in_worker() -> None:
    """A cancelled future must not cancel later, independent ``unkoil`` calls.

    The calls are nested inside a :func:`run_threaded` worker on purpose: that
    is the only place an *ambient* ``current_cancel_event`` exists, and thus the
    only place the old shared-event design could leak a cancellation across
    calls. (The same body on the bare main thread passes on the buggy code too,
    because each main-thread ``unkoil`` gets a fresh per-call event — which is
    why the plan's literal main-thread Test A does not reproduce the bug.)
    """

    captured: dict[str, object] = {}

    with Koil():

        async def body() -> None:
            def worker() -> None:
                # Inside a run_threaded worker -> current_cancel_event is set,
                # so every nested unkoil here shares that ambient event.
                async def slow() -> None:
                    await asyncio.sleep(5)

                fut = unkoil_task(slow)
                fut.cancel(reason="simulated keyboard interrupt")
                with pytest.raises(
                    (
                        asyncio.CancelledError,
                        concurrent.futures.CancelledError,
                        ThreadCancelledError,
                    )
                ):
                    fut.result()

                # The bug: this independent call used to raise
                # ThreadCancelledError (the ambient event was left set). It must
                # run to completion instead.
                async def teardown() -> str:
                    await asyncio.sleep(0)
                    return "ok"

                captured["second"] = unkoil(teardown)

            await run_threaded(worker)

        unkoil(body)

    assert captured["second"] == "ok"


@pytest.mark.timeout(10)
def test_genuine_ambient_cancel_still_propagates() -> None:
    """Regression guard: a *real* scope cancellation must still cancel nested work.

    The poisoning fix separates a future's own cancel event from the ambient
    one but must keep racing against the ambient event, so that cancelling a
    ``run_threaded`` worker still aborts ``unkoil`` calls nested inside it.
    """

    started = threading.Event()

    async def outer() -> None:
        def worker() -> None:
            started.set()

            async def slow() -> str:
                await asyncio.sleep(30)
                return "should not get here"

            # When the enclosing worker is cancelled the ambient event fires;
            # this nested unkoil must observe it and not block for 30s.
            unkoil(slow)

        await run_threaded(worker)

    async def driver() -> None:
        task = asyncio.create_task(outer())
        # Wait until the worker is actually running before cancelling.
        while not started.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with Koil():
        unkoil(driver)


# --------------------------------------------------------------------------- #
# Bug #2 — double-close of an async generator must not raise                   #
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(10)
def test_double_close_of_async_gen_is_safe() -> None:
    """Closing a generator whose ``__anext__`` is still unwinding is idempotent.

    Reproduces the two-path teardown: cancel an in-flight ``__anext__`` (path 1)
    and immediately close the same generator (path 2). A raw ``aclose()`` raises
    ``RuntimeError("... already running")`` here; the koil helper swallows it,
    and the generator's ``finally`` still runs exactly once.
    """

    cleanup = {"count": 0}

    async def follow() -> "asyncio.AsyncGenerator[int, None]":
        try:
            while True:
                await asyncio.sleep(1.0)
                yield 1
        finally:
            cleanup["count"] += 1

    with Koil():
        loop = get_koiled_loop_or_raise()
        ait = follow().__aiter__()

        # Start a step and let it actually begin running on the loop so the
        # generator frame is "running" (suspended inside the inner sleep).
        fut = run_async_sharing_context(ait.__anext__, loop, None)
        time.sleep(0.1)

        # Path 1: cancel the in-flight __anext__.
        fut.cancel(reason="path 1")
        # Path 2: immediately close, racing path 1's unwind. Must not raise.
        aclose_async_gen_threadsafe(ait, loop)

    assert cleanup["count"] == 1


@pytest.mark.timeout(10)
def test_raw_aclose_during_anext_raises_demonstrates_bug() -> None:
    """Documents the underlying CPython behaviour the helper exists to absorb.

    A *raw* ``aclose()`` submitted while ``__anext__`` is still running on the
    loop raises ``RuntimeError: aclose(): asynchronous generator is already
    running``. This is exactly what :func:`aclose_async_gen_threadsafe`
    swallows.
    """

    async def follow() -> "asyncio.AsyncGenerator[int, None]":
        while True:
            await asyncio.sleep(1.0)
            yield 1

    with Koil():
        loop = get_koiled_loop_or_raise()
        ait = follow().__aiter__()
        anext_future = asyncio.run_coroutine_threadsafe(ait.__anext__(), loop)
        time.sleep(0.1)  # let __anext__ begin running

        close_future = asyncio.run_coroutine_threadsafe(ait.aclose(), loop)
        with pytest.raises(RuntimeError, match="already running"):
            close_future.result(timeout=5)

        anext_future.cancel()


@pytest.mark.timeout(10)
def test_unkoil_gen_interrupt_closes_generator_promptly() -> None:
    """Interrupting ``unkoil_gen`` mid-iteration runs the generator's finally.

    The generator's ``finally`` must run as part of closing the consumer (within
    the ``with Koil()`` block), deterministically, without raising the
    double-close ``RuntimeError``.
    """

    cleanup = {"count": 0}

    async def follow() -> "asyncio.AsyncGenerator[int, None]":
        try:
            i = 0
            while True:
                yield i
                i += 1
                await asyncio.sleep(0.01)
        finally:
            cleanup["count"] += 1

    from koil.bridge import unkoil_gen

    with Koil():
        gen = unkoil_gen(follow)
        assert next(gen) == 0
        assert next(gen) == 1
        # Consumer stops early -> GeneratorExit -> unkoil_gen finally closes the
        # async generator on the loop.
        gen.close()
        assert cleanup["count"] == 1


# --------------------------------------------------------------------------- #
# Bug #1 (end-to-end) — real SIGINT during a blocking unkoil still tears down  #
# --------------------------------------------------------------------------- #


_CHILD_PROGRAM = textwrap.dedent(
    """
    import asyncio
    import signal
    import sys

    # REQUIRED: a process launched non-interactively may inherit SIG_IGN for
    # SIGINT, which survives exec; restore the default handler so the parent's
    # kill -INT is actually delivered as a KeyboardInterrupt.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    from koil import Koil
    from koil.bridge import unkoil

    def main():
        # Mirrors a koilable context manager: the body blocks in unkoil on the
        # main thread; Ctrl+C propagates out, and teardown runs as another
        # main-thread unkoil (as __exit__ -> unkoil(__aexit__) would). The
        # teardown must complete and not hang or raise a spurious cancellation.
        with Koil():
            async def long_running():
                await asyncio.sleep(60)

            async def teardown():
                await asyncio.sleep(0)
                return "ok"

            try:
                print("READY", flush=True)
                unkoil(long_running)
            except KeyboardInterrupt:
                assert unkoil(teardown) == "ok"
                print("teardown-done", flush=True)
                sys.exit(130)

    main()
    """
)


@pytest.mark.timeout(30)
@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT semantics")
def test_sigint_during_blocking_unkoil_allows_clean_teardown() -> None:
    """Real Ctrl+C while blocked in ``unkoil`` still lets teardown complete."""

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_PROGRAM],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Wait for the readiness marker so SIGINT lands while truly blocked.
        deadline = time.time() + 10
        assert proc.stdout is not None
        ready = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "READY" in line:
                ready = True
                break
        assert ready, "child never signalled READY"

        proc.send_signal(signal.SIGINT)

        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"child hung after SIGINT (no clean teardown); output:\n{out}")

    assert "teardown-done" in out, f"teardown did not complete; output:\n{out}"
    assert proc.returncode == 130, f"unexpected exit code {proc.returncode}; output:\n{out}"
