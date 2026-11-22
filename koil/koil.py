"""Deprecated backward-compatibility shim — import from :mod:`koil.loop` instead.

Accessing any name on this module emits a :class:`DeprecationWarning` and
returns the object from its new home.
"""

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # What the names below resolve to, for type checkers; at runtime
    # __getattr__ imports them lazily and warns.
    from koil.loop import Koil as Koil
    from koil.loop import KoilProtocol as KoilProtocol
    from koil.loop import _new_event_loop as _new_event_loop
    from koil.loop import get_threaded_loop as get_threaded_loop
    from koil.loop import run_threaded_event_loop as run_threaded_event_loop

# name -> (target module, target name)
_DEPRECATED: dict[str, tuple[str, str]] = {
    "Koil": ("koil.loop", "Koil"),
    "KoilProtocol": ("koil.loop", "KoilProtocol"),
    "get_threaded_loop": ("koil.loop", "get_threaded_loop"),
    "run_threaded_event_loop": ("koil.loop", "run_threaded_event_loop"),
    "_new_event_loop": ("koil.loop", "_new_event_loop"),
}

__all__ = [name for name in _DEPRECATED if not name.startswith("_")]


def __getattr__(name: str) -> object:
    try:
        module, target = _DEPRECATED[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warnings.warn(
        f"koil.koil.{name} is deprecated; import {target} from {module} instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module), target)
