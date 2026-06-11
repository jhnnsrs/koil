"""Deprecated backward-compatibility shim — import from :mod:`koil.bridge` instead.

Accessing any name on this module emits a :class:`DeprecationWarning` and
returns the object from its new home. The ``run_spawned`` / ``iterate_spawned``
aliases point at their renamed counterparts ``run_threaded`` /
``iterate_threaded``.
"""

import importlib
import warnings
from typing import Any, Dict, Tuple

# name -> (target module, target name)
_DEPRECATED: Dict[str, Tuple[str, str]] = {
    "get_koiled_loop_or_raise": ("koil.bridge", "get_koiled_loop_or_raise"),
    "sleep": ("koil.bridge", "sleep"),
    "unkoil": ("koil.bridge", "unkoil"),
    "unkoil_gen": ("koil.bridge", "unkoil_gen"),
    "unkoil_task": ("koil.bridge", "unkoil_task"),
    "run_threaded": ("koil.bridge", "run_threaded"),
    "run_threaded_bridged": ("koil.bridge", "run_threaded_bridged"),
    "iterate_threaded": ("koil.bridge", "iterate_threaded"),
    "iterate_threaded_bridged": ("koil.bridge", "iterate_threaded_bridged"),
    "run_spawned": ("koil.bridge", "run_threaded"),
    "iterate_spawned": ("koil.bridge", "iterate_threaded"),
    "KOIL_CANCEL_TIMEOUT": ("koil.bridge", "KOIL_CANCEL_TIMEOUT"),
    "KoilThreadSafeEvent": ("koil.context", "KoilThreadSafeEvent"),
}

_RENAMED = {
    "run_spawned": "run_threaded",
    "iterate_spawned": "iterate_threaded",
}

__all__ = list(_DEPRECATED)


def __getattr__(name: str) -> Any:
    try:
        module, target = _DEPRECATED[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    if name in _RENAMED:
        message = (
            f"koil.helpers.{name} has been renamed; "
            f"import {target} from {module} instead"
        )
    else:
        message = f"koil.helpers.{name} is deprecated; import {target} from {module} instead"
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    return getattr(importlib.import_module(module), target)
