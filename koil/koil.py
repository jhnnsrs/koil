"""Backward-compatibility shim — import from :mod:`koil.loop` instead."""
from koil.loop import Koil, KoilProtocol, get_threaded_loop, run_threaded_event_loop, _new_event_loop

__all__ = ["Koil", "KoilProtocol", "get_threaded_loop", "run_threaded_event_loop", "_new_event_loop"]
