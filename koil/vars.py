"""Deprecated backward-compatibility shim — import from :mod:`koil.context` instead.

Accessing any name on this module emits a :class:`DeprecationWarning` and
returns the object from its new home.
"""

import importlib
import warnings
from typing import Any, Dict, Tuple

# name -> (target module, target name)
_DEPRECATED: Dict[str, Tuple[str, str]] = {
    "KoiledLoop": ("koil.context", "KoiledLoop"),
    "KoilThreadSafeEvent": ("koil.context", "KoilThreadSafeEvent"),
    "global_koil": ("koil.context", "global_koil"),
    "global_koil_loop": ("koil.context", "global_koil_loop"),
    "current_koil": ("koil.context", "current_koil"),
    "current_cancel_event": ("koil.context", "current_cancel_event"),
    "check_cancelled": ("koil.context", "check_cancelled"),
}

__all__ = list(_DEPRECATED)


def __getattr__(name: str) -> Any:
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
