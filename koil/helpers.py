"""Backward-compatibility shim — import from :mod:`koil.bridge` instead."""
from koil.bridge import (
    get_koiled_loop_or_raise,
    sleep,
    unkoil,
    unkoil_gen,
    unkoil_task,
    run_threaded,
    iterate_threaded,
    KOIL_CANCEL_TIMEOUT,
)
from koil.context import KoilThreadSafeEvent

# Legacy aliases
run_spawned = run_threaded
iterate_spawned = iterate_threaded

__all__ = [
    "get_koiled_loop_or_raise",
    "sleep",
    "unkoil",
    "unkoil_gen",
    "unkoil_task",
    "run_threaded",
    "iterate_threaded",
    "run_spawned",
    "iterate_spawned",
    "KOIL_CANCEL_TIMEOUT",
    "KoilThreadSafeEvent",
]
