"""Traceback rewriting at koil's sync<->async boundaries.

When an exception raised in user code crosses a koil bridge (e.g. an async
function called via :func:`~koil.bridge.unkoil`, or a sync function run via
:func:`~koil.bridge.run_threaded`), its traceback accumulates frames from
koil's own machinery plus the ``concurrent.futures`` glue that carries the
exception between threads. Those frames are transport noise: they say nothing
about *what* failed, only *how* the failure travelled.

This module rewrites ``exc.__traceback__`` in place at the bridge boundaries,
dropping the machinery frames while keeping exactly one koil frame (the
boundary function the user actually called) as a marker that the error
crossed a sync<->async bridge.

Frames are dropped when:

* their function body sets a truthy ``__tracebackhide__`` local — the pytest
  convention, applied to koil's internal wrapper functions, so pytest's own
  formatter hides them too; or
* their module is one of the ``concurrent.futures`` glue modules in
  :data:`_HIDDEN_MODULES`. The list is deliberately narrow and never includes
  ``asyncio.*`` — user code legitimately appears in asyncio frames
  (``gather``, ``wait_for``, ...), and empirically no asyncio frames occur on
  koil's bridge paths anyway (the Task machinery is C-level).

Rewriting is on by default and controlled by:

* ``Koil(rewrite_tracebacks=False)`` per koil instance;
* :data:`REWRITE_TRACEBACKS` as the module-wide default when no koil
  instance is ambient;
* the ``KOIL_FULL_TRACEBACK=1`` environment variable, which forces full
  tracebacks regardless of the above — the escape hatch for debugging koil
  itself.
"""

import os
import types
from typing import List, Optional, Set, Tuple

from koil.context import global_koil

#: Module-wide default used when no :class:`~koil.loop.Koil` instance is
#: ambient (e.g. exceptions pruned on the loop side before being emitted to Qt
#: signal handlers). Reassign (``koil.tracebacks.REWRITE_TRACEBACKS = False``)
#: to change the process-wide default.
REWRITE_TRACEBACKS: bool = True

#: stdlib glue modules whose frames are koil-transport noise. Deliberately
#: NARROW: never list asyncio.* here — user code legitimately appears in
#: asyncio frames (gather, wait_for, ...), and empirically no asyncio frames
#: occur on koil's bridge paths anyway.
_HIDDEN_MODULES = frozenset(
    {
        "concurrent.futures._base",
        "concurrent.futures.thread",
    }
)

_ENV_VAR = "KOIL_FULL_TRACEBACK"


def should_rewrite() -> bool:
    """Return whether tracebacks should be rewritten in the current context.

    Precedence: the ``KOIL_FULL_TRACEBACK`` environment variable (truthy ⇒
    never rewrite) wins over everything; otherwise the ambient
    :class:`~koil.loop.Koil` instance's ``rewrite_tracebacks`` attribute is
    consulted; with no ambient koil, :data:`REWRITE_TRACEBACKS` applies.
    """
    if os.environ.get(_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    koil = global_koil.get()
    if koil is not None:
        return bool(getattr(koil, "rewrite_tracebacks", REWRITE_TRACEBACKS))
    return REWRITE_TRACEBACKS


def _is_hidden(tb: types.TracebackType) -> bool:
    """Return ``True`` if this traceback entry is koil-transport noise."""
    frame = tb.tb_frame
    if frame.f_locals.get("__tracebackhide__"):
        return True
    return frame.f_globals.get("__name__") in _HIDDEN_MODULES


def prune_traceback(exc: BaseException, *, keep_boundary: bool = False) -> None:
    """Rewrite ``exc.__traceback__`` in place, dropping koil-internal frames.

    Args:
        exc: The exception whose traceback (and chained ``__cause__`` /
            ``__context__`` tracebacks) should be pruned.
        keep_boundary: Keep the head traceback entry unconditionally. At a
            catch site the head entry is the catching (boundary) frame's own
            entry, and a subsequent bare ``raise`` does *not* re-append it —
            so the boundary must be preserved here for exactly one koil frame
            to survive as a marker.

    If pruning would remove every informative frame (i.e. the error
    originated inside koil itself, like a cancel-timeout
    :class:`~koil.errors.KoilError`), the traceback is left untouched —
    koil's frames *are* the informative part there.

    Never raises: traceback cosmetics must not mask the real error.
    """
    seen: Set[int] = set()
    stack: List[Tuple[Optional[BaseException], bool]] = [(exc, keep_boundary)]
    while stack:
        current, keep = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        try:
            _prune_one(current, keep)
        except Exception:
            pass
        stack.append((current.__cause__, False))
        stack.append((current.__context__, False))


def _prune_one(exc: BaseException, keep_boundary: bool) -> None:
    tb = exc.__traceback__
    if tb is None:
        return

    kept: List[types.TracebackType] = []
    index = 0
    while tb is not None:
        if (index == 0 and keep_boundary) or not _is_hidden(tb):
            kept.append(tb)
        tb = tb.tb_next
        index += 1

    if len(kept) <= (1 if keep_boundary else 0):
        # Nothing beyond the boundary marker survives: the error originated
        # inside koil's own machinery, so its frames are the informative part.
        return

    new_tb: Optional[types.TracebackType] = None
    for entry in reversed(kept):
        new_tb = types.TracebackType(
            new_tb, entry.tb_frame, entry.tb_lasti, entry.tb_lineno
        )
    exc.__traceback__ = new_tb
