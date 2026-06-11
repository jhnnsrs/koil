"""Tests for koil's boundary traceback rewriting (koil/tracebacks.py).

Exceptions that cross a koil bridge should keep the user's frames plus exactly
one koil "boundary" frame as a marker, with the machinery frames (koil
internals and concurrent.futures glue) pruned away. ``rewrite_tracebacks=False``
and ``KOIL_FULL_TRACEBACK=1`` restore the full traceback.
"""

import os
import traceback

import pytest

import koil as koil_package
from koil import Koil
from koil.bridge import (
    iterate_threaded,
    run_threaded,
    run_threaded_bridged,
    unkoil,
    unkoil_gen,
    unkoil_task,
)
from koil.tracebacks import prune_traceback


# The actual installed package directory — a substring check like
# "/koil/koil/" breaks on CI, where the checkout root is .../koil/koil and
# therefore *every* repo file (this test included) matches.
KOIL_DIR = os.path.dirname(os.path.abspath(koil_package.__file__)) + os.sep


def frames(e: BaseException):
    return traceback.extract_tb(e.__traceback__)


def koil_frames(e: BaseException):
    return [f for f in frames(e) if os.path.abspath(f.filename).startswith(KOIL_DIR)]


def futures_frames(e: BaseException):
    return [f for f in frames(e) if "concurrent/futures" in f.filename.replace("\\", "/")]


async def user_async():
    raise ValueError("boom")


async def user_mid():
    return await user_async()


def test_unkoil_prunes_machinery():
    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil(user_mid)

    e = excinfo.value
    assert not futures_frames(e)
    kf = koil_frames(e)
    assert [f.name for f in kf] == ["unkoil"]
    user_names = [f.name for f in frames(e) if f not in kf]
    assert user_names[-2:] == ["user_mid", "user_async"]


def test_unkoil_gen_prunes():
    async def agen():
        yield 1
        raise ValueError("boom in gen")

    with Koil():
        with pytest.raises(ValueError) as excinfo:
            for _ in unkoil_gen(agen):
                pass

    e = excinfo.value
    assert not futures_frames(e)
    assert [f.name for f in koil_frames(e)] == ["unkoil_gen"]
    assert frames(e)[-1].name == "agen"


def test_unkoil_task_result_boundary():
    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil_task(user_mid).result()

    e = excinfo.value
    assert not futures_frames(e)
    assert [f.name for f in koil_frames(e)] == ["result"]
    assert frames(e)[-1].name == "user_async"


@pytest.mark.parametrize("runner", [run_threaded, run_threaded_bridged])
def test_run_threaded_prunes(runner):
    def user_sync():
        raise ValueError("boom in thread")

    async def main():
        await runner(user_sync)

    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil(main)

    e = excinfo.value
    assert not futures_frames(e)
    names = [f.name for f in frames(e)]
    assert "wrapper" not in names
    assert "body" not in names
    assert "_run_threaded" not in names
    assert names[-1] == "user_sync"
    kf = [f.name for f in koil_frames(e)]
    assert kf == ["unkoil", runner.__name__]
    assert e.__context__ is None


def test_iterate_threaded_prunes():
    def user_gen():
        yield 1
        raise ValueError("boom in gen step")

    async def main():
        async for _ in iterate_threaded(user_gen):
            pass

    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil(main)

    e = excinfo.value
    names = [f.name for f in frames(e)]
    assert "step" not in names
    assert "wrapper" not in names
    assert names[-1] == "user_gen"
    assert [f.name for f in koil_frames(e)] == ["unkoil", "_iterate_threaded"]


def test_iterate_threaded_close_path_prunes():
    def user_gen():
        try:
            yield 1
            yield 2
        finally:
            raise ValueError("boom in finally")

    async def main():
        agen = iterate_threaded(user_gen)
        await agen.__anext__()
        # Explicit aclose throws GeneratorExit into the generator now (a bare
        # `break` defers finalization to the loop's asyncgen hooks).
        await agen.aclose()

    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil(main)

    e = excinfo.value
    names = [f.name for f in frames(e)]
    assert "wrapper" not in names
    assert "body" not in names
    assert names[-1] == "user_gen"


def test_stopiteration_wrap_chain():
    def user_sync():
        raise StopIteration("manual stop")

    async def main():
        await run_threaded(user_sync)

    with Koil():
        with pytest.raises(RuntimeError, match="StopIteration") as excinfo:
            unkoil(main)

    e = excinfo.value
    cause = e.__cause__
    assert isinstance(cause, StopIteration)
    cause_names = [f.name for f in traceback.extract_tb(cause.__traceback__)]
    assert "wrapper" not in cause_names
    assert cause_names[-1] == "user_sync"
    # The RuntimeError itself originates inside koil; the keep-full fallback
    # leaves its own (koil-internal) traceback intact.
    assert e.__traceback__ is not None


def test_flag_off_restores_full_frames():
    with Koil(rewrite_tracebacks=False):
        with pytest.raises(ValueError) as excinfo:
            unkoil(user_mid)

    e = excinfo.value
    assert futures_frames(e)
    names = [f.name for f in frames(e)]
    assert "result" in names
    assert "passed_with_context" in names


def test_env_var_forces_full(monkeypatch):
    monkeypatch.setenv("KOIL_FULL_TRACEBACK", "1")
    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil(user_mid)

    e = excinfo.value
    assert futures_frames(e)
    assert "passed_with_context" in [f.name for f in frames(e)]


def test_chained_user_exceptions():
    async def chained():
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise ValueError("outer") from inner

    with Koil():
        with pytest.raises(ValueError) as excinfo:
            unkoil(chained)

    e = excinfo.value
    assert isinstance(e.__cause__, KeyError)
    assert e.__suppress_context__ is True
    assert e.__context__ is e.__cause__
    assert frames(e)[-1].name == "chained"
    cause_frames = traceback.extract_tb(e.__cause__.__traceback__)
    assert cause_frames[-1].name == "chained"
    assert not [
        f for f in cause_frames if "concurrent/futures" in f.filename.replace("\\", "/")
    ]


def test_prune_cycle_safe():
    e1 = ValueError("a")
    e2 = KeyError("b")
    e1.__context__ = e2
    e2.__context__ = e1
    prune_traceback(e1)  # must terminate


def test_prune_keep_full_fallback():
    # An exception whose every frame is hidden keeps its traceback untouched:
    # the error originated inside koil, so its frames are the informative part.
    def hidden_raiser():
        __tracebackhide__ = True
        raise ValueError("koil internal")

    def hidden_catcher():
        __tracebackhide__ = True
        try:
            hidden_raiser()
        except ValueError as e:
            return e

    e = hidden_catcher()
    original_names = [f.name for f in frames(e)]
    assert original_names == ["hidden_catcher", "hidden_raiser"]

    prune_traceback(e)
    assert [f.name for f in frames(e)] == original_names

    prune_traceback(e, keep_boundary=True)
    assert [f.name for f in frames(e)] == original_names


def test_cancellation_unaffected():
    import asyncio

    async def hang():
        await asyncio.sleep(30)

    with Koil():
        future = unkoil_task(hang)
        assert future.cancel(reason="test")
        with pytest.raises(BaseException) as excinfo:
            future.result()
    assert "Cancelled" in type(excinfo.value).__name__
