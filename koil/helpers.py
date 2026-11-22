"""Deprecated backward-compatibility shim — import from :mod:`koil.bridge` instead.

Accessing any name on this module emits a :class:`DeprecationWarning` and
returns the object from its new home. The ``run_spawned`` / ``iterate_spawned``
aliases point at their renamed counterparts ``run_threaded`` /
``iterate_threaded``.
"""

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # What the names below resolve to, for type checkers; at runtime
    # __getattr__ imports them lazily and warns.
    from koil.bridge import (
        KOIL_CANCEL_TIMEOUT as KOIL_CANCEL_TIMEOUT,
    )
    from koil.bridge import (
        get_koiled_loop_or_raise as get_koiled_loop_or_raise,
    )
    from koil.bridge import iterate_threaded as iterate_threaded
    from koil.bridge import (
        iterate_threaded_bridged as iterate_threaded_bridged,
    )
    from koil.bridge import run_threaded as run_threaded
    from koil.bridge import run_threaded_bridged as run_threaded_bridged
    from koil.bridge import sleep as sleep
    from koil.bridge import unkoil as unkoil
    from koil.bridge import unkoil_gen as unkoil_gen
    from koil.bridge import unkoil_task as unkoil_task
    from koil.context import KoilThreadSafeEvent as KoilThreadSafeEvent

    run_spawned = run_threaded
    iterate_spawned = iterate_threaded

# name -> (target module, target name)
_DEPRECATED: dict[str, tuple[str, str]] = {
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


def __getattr__(name: str) -> object:
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
        message = (
            f"koil.helpers.{name} is deprecated; import {target} from {module} instead"
        )
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    return getattr(importlib.import_module(module), target)
