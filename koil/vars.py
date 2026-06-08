"""Backward-compatibility shim — import from :mod:`koil.context` instead."""
from koil.context import (
    KoiledLoop,
    KoilThreadSafeEvent,
    global_koil,
    global_koil_loop,
    current_koil,
    current_cancel_event,
    check_cancelled,
)

__all__ = [
    "KoiledLoop",
    "KoilThreadSafeEvent",
    "global_koil",
    "global_koil_loop",
    "current_koil",
    "current_cancel_event",
    "check_cancelled",
]
