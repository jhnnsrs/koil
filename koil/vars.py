"""Deprecated backward-compatibility shim — import from :mod:`koil.context` instead.

Accessing any name on this module emits a :class:`DeprecationWarning` and
returns the object from its new home.
"""

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # What the names below resolve to, for type checkers; at runtime
    # __getattr__ imports them lazily and warns.
    from koil.context import KoiledLoop as KoiledLoop
    from koil.context import KoilThreadSafeEvent as KoilThreadSafeEvent
    from koil.context import check_cancelled as check_cancelled
    from koil.context import current_cancel_event as current_cancel_event
    from koil.context import current_koil as current_koil
    from koil.context import global_koil as global_koil
    from koil.context import global_koil_loop as global_koil_loop

# name -> (target module, target name)
_DEPRECATED: dict[str, tuple[str, str]] = {
    "KoiledLoop": ("koil.context", "KoiledLoop"),
    "KoilThreadSafeEvent": ("koil.context", "KoilThreadSafeEvent"),
    "global_koil": ("koil.context", "global_koil"),
    "global_koil_loop": ("koil.context", "global_koil_loop"),
    "current_koil": ("koil.context", "current_koil"),
    "current_cancel_event": ("koil.context", "current_cancel_event"),
    "check_cancelled": ("koil.context", "check_cancelled"),
}

__all__ = list(_DEPRECATED)


def __getattr__(name: str) -> object:
    try:
        module, target = _DEPRECATED[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warnings.warn(
        f"koil.vars.{name} is deprecated; import {target} from {module} instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module), target)
