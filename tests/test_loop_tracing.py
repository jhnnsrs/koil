"""Tests for step-debugger visibility of the koil loop thread (koil/loop.py).

The background loop thread is traced by the debugger by default so breakpoints
inside coroutines running on it are hit. ``KOIL_DO_TRACE`` set to a falsy value
opts out, hiding the thread from pydevd/debugpy again. The ``pydev_do_not_trace``
and ``is_pydev_daemon_thread`` markers are pydevd conventions, so we assert on
them directly rather than driving a real debugger.
"""

import asyncio

import pytest

from koil.loop import get_threaded_loop


def _make_loop():
    """Create a threaded loop and return ``(loop, thread)``; caller must stop it."""
    return get_threaded_loop(name="TracingTestLoop", uvify=False)


def _stop(loop: asyncio.AbstractEventLoop, thread) -> None:
    """Stop the loop and join its thread so the loop is closed before returning."""
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5.0)


def test_loop_thread_is_traced_by_default(monkeypatch):
    monkeypatch.delenv("KOIL_DO_TRACE", raising=False)
    loop, thread = _make_loop()
    try:
        assert thread.pydev_do_not_trace is False
        assert thread.is_pydev_daemon_thread is False
    finally:
        _stop(loop, thread)


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF", " off "])
def test_falsy_koil_do_trace_hides_loop_thread(monkeypatch, value):
    monkeypatch.setenv("KOIL_DO_TRACE", value)
    loop, thread = _make_loop()
    try:
        assert thread.pydev_do_not_trace is True
        assert thread.is_pydev_daemon_thread is True
    finally:
        _stop(loop, thread)


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_truthy_koil_do_trace_keeps_loop_thread_traced(monkeypatch, value):
    monkeypatch.setenv("KOIL_DO_TRACE", value)
    loop, thread = _make_loop()
    try:
        assert thread.pydev_do_not_trace is False
        assert thread.is_pydev_daemon_thread is False
    finally:
        _stop(loop, thread)
