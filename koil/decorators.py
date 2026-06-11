import asyncio
import functools
import inspect
import logging
from types import TracebackType
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    Callable,
    ContextManager,
    Coroutine,
    Generator,
    Generic,
    ParamSpec,
    Protocol,
    Self,
    Type,
    TypeVar,
    Union,
    overload,
)

from koil.bridge import (
    unkoil,
    unkoil_gen,
    unkoil_gen_with_timeout,
    unkoil_with_timeout,
)
from koil.context import global_koil
from koil.loop import Koil, KoilProtocol


class Koilable(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


class SyncKoilable(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


T = TypeVar("T", bound=Koilable)
KoilType = TypeVar("KoilType", bound=KoilProtocol)


logger = logging.getLogger(__name__)


@overload
def koilable(maybe_cls: Type[T]) -> Type[T]: ...


@overload
def koilable(
    maybe_cls: Union[str, None] = None,
    add_connectors: bool = False,
    fieldname: str = "__koil",
) -> Callable[[Type[T]], Type[T]]: ...


def koilable(
    maybe_cls: Union[Type[T], str, None] = None,
    add_connectors: bool = False,
    fieldname: str = "__koil",
) -> Union[Type[T], Callable[[Type[T]], Type[T]]]:
    """
    Decorator to make an async context manager class usable from sync code.

    Can be applied bare (``@koilable``) or with arguments
    (``@koilable(add_connectors=True)``).

    Args:
        maybe_cls: The decorated class when used bare; a fieldname string when
            called positionally (legacy form); ``None`` in the factory form.
        add_connectors (bool, optional): If True, it will add the connectors to the class. Defaults to False.
        fieldname (str, optional): The name of the field to store the koil instance. Defaults to "__koil".

    Note on tracebacks: the generated ``__enter__``/``__exit__`` wrappers are
    marked with ``__tracebackhide__`` so pytest hides them, but plain Python
    tracebacks through ``with MyKoilable():`` show two koil frames (the
    wrapper plus ``unkoil``) — the wrapper sits above the pruning boundary in
    :func:`~koil.bridge.unkoil`, and adding another catch-and-reraise here
    would add a frame rather than remove one.
    """
    if isinstance(maybe_cls, str):
        # Legacy positional form: koilable("_my_field", add_connectors)
        fieldname = maybe_cls
        maybe_cls = None

    def real_cls_decorator(cls: Type[T]) -> Type[T]:
        if not hasattr(cls, "__aenter__"):
            raise TypeError(f"@koilable class {cls.__name__} must implement __aenter__")
        if not hasattr(cls, "__aexit__"):
            raise TypeError(f"@koilable class {cls.__name__} must implement __aexit__")
        if not inspect.iscoroutinefunction(cls.__aenter__):
            raise TypeError(
                f"{cls.__name__}.__aenter__ must be a coroutine function (async def)"
            )
        if not inspect.iscoroutinefunction(cls.__aexit__):
            raise TypeError(
                f"{cls.__name__}.__aexit__ must be a coroutine function (async def)"
            )
        setattr(cls, "__koilable__", True)

        def koiled_enter(self: Koilable):
            __tracebackhide__ = True
            potential_koiled_loop = global_koil.get()
            if potential_koiled_loop is not None:
                # We are in a koiled loop no need to koil again
                return unkoil(self.__aenter__)
            else:
                if not hasattr(self, fieldname) or getattr(self, fieldname) is None:
                    setattr(self, fieldname, Koil())

                koil = getattr(self, fieldname)
                koil.__enter__()
                try:
                    return unkoil(self.__aenter__)
                except BaseException:
                    # Don't leak the just-started Koil (and its loop thread)
                    # when __aenter__ fails — __exit__ will never run.
                    koil.__exit__(None, None, None)
                    raise

        def koiled_exit(
            self: Koilable,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ):
            __tracebackhide__ = True
            unkoil(self.__aexit__, exc_type, exc_val, exc_tb)
            koil = getattr(self, fieldname, None)
            if koil is not None:
                koil.__exit__(None, None, None)

        def koiled_param_exit(self: Koilable):
            koiled_exit(self, None, None, None)

        async def aexit(self: Koilable):
            return await self.__aexit__(None, None, None)

        async def aenter(self: Koilable):
            return await self.__aenter__()

        setattr(cls, "__enter__", koiled_enter)
        setattr(cls, "__exit__", koiled_exit)
        if add_connectors:
            setattr(cls, "aexit", aexit)
            setattr(cls, "aenter", aenter)
            setattr(cls, "exit", koiled_param_exit)
            setattr(cls, "enter", koiled_enter)

        return cls

    if maybe_cls is not None:
        # Bare @koilable form
        return real_cls_decorator(maybe_cls)

    return real_cls_decorator


P = ParamSpec("P")
R = TypeVar("R")
Y = TypeVar("Y")
S = TypeVar("S")


def _in_async_context() -> bool:
    """True when the calling frame can ``await`` (a loop is running here)."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@overload
def koiled(maybe_fn: Callable[P, AsyncGenerator[Y, S]]) -> Callable[P, Generator[Y, S, None]]: ...


@overload
def koiled(maybe_fn: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]: ...


@overload
def koiled(
    maybe_fn: None = None, *, timeout: float | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def koiled(
    maybe_fn: Callable[..., Any] | None = None, *, timeout: float | None = None
) -> Callable[..., Any]:
    """Make an ``async def`` function callable from both sync and async code.

    The wrapped function behaves like the original in async contexts (calling
    it returns the coroutine / async generator for the caller to ``await`` /
    ``async for``), while a call from a sync context runs it on the ambient
    koil loop via :func:`~koil.bridge.unkoil` (or
    :func:`~koil.bridge.unkoil_gen` for async generators) and blocks for the
    result. This replaces the hand-written one-line sync wrappers
    (``def get(self): return unkoil(self.fetch)``) that sync-facing APIs
    otherwise need for every method.

    Can be applied bare (``@koiled``) or with arguments
    (``@koiled(timeout=5)``); *timeout* bounds sync calls via
    :func:`~koil.bridge.unkoil_with_timeout` /
    :func:`~koil.bridge.unkoil_gen_with_timeout` and is ignored in async
    contexts (use ``asyncio.timeout`` there).

    A sync call requires an active koil context (``with Koil():`` or an
    entered ``@koilable`` object); the decorator deliberately does not start a
    ``Koil`` of its own, since a bare function call has no scope in which to
    tear the loop down again.

    Instance methods work as usual (the descriptor protocol binds ``self``
    before the wrapper runs); ``@classmethod`` / ``@staticmethod`` must be
    applied *above* ``@koiled``.

    Typing: the wrapper is typed from the sync caller's point of view
    (``Callable[P, R]``). For fully-typed async use, the undecorated original
    is available as ``wrapped.aio`` (gRPC-style): ``await fetch.aio(...)``.
    At runtime ``await fetch(...)`` works in async contexts too.

    Caveat: sync code running *on a thread whose frame has a running loop*
    (the ``sync_in_async=True`` escape hatch) is indistinguishable from async
    code here and gets the coroutine back; call :func:`~koil.bridge.unkoil`
    explicitly in that situation.
    """
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if isinstance(fn, (classmethod, staticmethod)):
            raise TypeError(
                "@koiled must be applied to the function itself; put "
                "@classmethod/@staticmethod above @koiled"
            )

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                __tracebackhide__ = True
                if _in_async_context():
                    return fn(*args, **kwargs)
                if timeout is not None:
                    return unkoil_gen_with_timeout(fn, timeout, *args, **kwargs)
                return unkoil_gen(fn, *args, **kwargs)

            gen_wrapper.aio = fn  # type: ignore[attr-defined]
            return gen_wrapper

        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                "@koiled requires an async def function or an async generator "
                f"function, got {fn!r}"
            )

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            __tracebackhide__ = True
            if _in_async_context():
                return fn(*args, **kwargs)
            if timeout is not None:
                return unkoil_with_timeout(fn, timeout, *args, **kwargs)
            return unkoil(fn, *args, **kwargs)

        wrapper.aio = fn  # type: ignore[attr-defined]
        return wrapper

    if maybe_fn is not None:
        # Bare @koiled form
        return decorate(maybe_fn)

    return decorate


TCM = TypeVar("TCM")


class _KoiledContextManager(Generic[TCM]):
    """Sync context manager driving an async context manager over the koil loop."""

    def __init__(
        self, acm: AsyncContextManager[TCM], koil: Koil | None = None
    ) -> None:
        self._acm = acm
        self._koil = koil
        self._own_koil: Koil | None = None

    def __enter__(self) -> TCM:
        __tracebackhide__ = True
        if global_koil.get() is None:
            self._own_koil = self._koil if self._koil is not None else Koil()
            self._own_koil.__enter__()
        try:
            return unkoil(self._acm.__aenter__)
        except BaseException:
            # Don't leak the just-started Koil when __aenter__ fails —
            # __exit__ will never run.
            if self._own_koil is not None:
                self._own_koil.__exit__(None, None, None)
                self._own_koil = None
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        __tracebackhide__ = True
        try:
            # Return __aexit__'s value so exception suppression propagates.
            return unkoil(self._acm.__aexit__, exc_type, exc_val, exc_tb)
        finally:
            if self._own_koil is not None:
                self._own_koil.__exit__(None, None, None)
                self._own_koil = None


def koiled_cm(
    acm: AsyncContextManager[TCM], koil: Koil | None = None
) -> ContextManager[TCM]:
    """Wrap an async context manager *instance* into a sync context manager.

    The contextlib-style counterpart to the :func:`koilable` class decorator:
    instead of decorating a class you own, wrap any existing async context
    manager object and use it in a plain ``with`` block. ``__aenter__`` /
    ``__aexit__`` are driven on the ambient koil loop; if no koil context is
    active, a :class:`~koil.loop.Koil` is started on enter and torn down on
    exit (including when ``__aenter__`` raises).

    Args:
        acm: The async context manager instance to wrap.
        koil: Optional pre-configured (not yet entered) :class:`Koil` to use
            when no ambient koil context exists. Ignored when an ambient koil
            is already active.

    Returns:
        A sync context manager whose ``with`` block yields whatever
        ``acm.__aenter__`` returned. Exception suppression by ``__aexit__``
        propagates.

    Example::

        from koil import koiled_cm

        with koiled_cm(httpx.AsyncClient()) as client:
            ...
    """
    return _KoiledContextManager(acm, koil)
