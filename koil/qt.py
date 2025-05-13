import asyncio
import contextvars
import inspect
import logging
from dataclasses import dataclass
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Dict,
    Generic,
    Tuple,
    TypeVar,
    Awaitable,
    Concatenate,
)
from koil.helpers import get_koiled_loop_or_raise
from koil.protocols import SignalProtocol
from qtpy import QtCore, QtWidgets
from typing_extensions import ParamSpec
from koil.koil import Koil
from koil.utils import (
    run_async_sharing_context,
    iterate_async_sharing_context,
)
import uuid
from .utils import  KoilFuture
from .errors import CancelledError

logger = logging.getLogger(__name__)


Reference = str


T = TypeVar("T")



# Your dynamic signal bundle
@dataclass
class QtSignalHolder(Generic[T]):
    returned: SignalProtocol[Tuple[T, contextvars.Context]]
    cancelled: SignalProtocol[CancelledError]
    errored: SignalProtocol[BaseException]


@dataclass
class QtIteratorSignalHolder(Generic[T]):
    done: SignalProtocol[None]
    cancelled: SignalProtocol[CancelledError]
    errored: SignalProtocol[BaseException]
    next: SignalProtocol[Tuple[T, contextvars.Context]]

def signal_builder(*args: Any) -> SignalProtocol[Any]:
    return QtCore.Signal(*args)  # type: ignore


class QtFuture(QtCore.QObject, Generic[T]):
    """A future that can be resolved in the Qt event loop

    Qt Futures are futures that can be resolved in the Qt event loop. They are
    useful for functions that need to be resolved in the Qt event loop and are
    not compatible with the asyncio event loop.

    QtFutures are generic and should be passed the type of the return value
    when creating the future. This is useful for type hinting and for the
    future to know what type of value to expect when it is resolved.



    """

    cancelled: "SignalProtocol[Exception]"
    cancelled = QtCore.Signal(Exception) # type: ignore

    def __init__(
        self,
        parent: QtCore.QObject | None = None,
    ):
        super().__init__(parent=parent)
        self.id = uuid.uuid4().hex
        self.loop = asyncio.get_event_loop()
        self.aiofuture: asyncio.Future[Tuple[contextvars.Context, T]] = asyncio.Future()
        self.iscancelled = False
        self.resolved = False
        self.rejected = False

    def set_cancelled(self):
        """WIll be called by the asyncio loop"""
        self.cancelled.emit()
        self.iscancelled = True

    @property
    def done(self):
        return self.resolved or self.rejected or self.iscancelled

    @property
    def is_cancelled(self):
        return self.aiofuture.cancelled()

    def resolve(self, *args: T):
        ctx = contextvars.copy_context()
        self.resolved = True

        if self.aiofuture.done():
            logger.warning(f"QtFuture {self} already done. Cannot resolve")
            return

        self.loop.call_soon_threadsafe(self.aiofuture.set_result, (ctx, args)) # type: ignore

    def reject(self, exp: Exception):
        if self.aiofuture.done():
            logger.warning(f"QtFuture {self} already done. Could not reject")
            return

        self.rejected = True

        self.loop.call_soon_threadsafe(self.aiofuture.set_exception, exp)


class QtStopIteration(Exception):
    pass


P = ParamSpec("P")


class QtGenerator(QtCore.QObject, Generic[T]):
    """A generator that can be run in the Qt event loop

    Qt Generators are generators that can be run in the Qt event loop. They are
    useful for functions that need to be run in the Qt event loop and are not
    compatible with the asyncio event loop.

    Qt Generators are generic and should be passed the type of the yield value
    when creating the generator. This is useful for type hinting and for the
    generator to know what type of value to expect when it is yielded.

    """

    def __init__(self):
        super().__init__()
        self.id = uuid.uuid4().hex
        self.loop = asyncio.get_event_loop()
        self.nextfuture: asyncio.Future[Tuple[contextvars.Context, T]] = (
            asyncio.Future()
        )
        self.iscancelled = False

    def set_cancelled(self):
        """WIll be called by the asyncio loop"""
        self.iscancelled = True

    def next(self, args: T):
        """Will be called by the asyncio loop"""
        context = contextvars.copy_context()

        self.loop.call_soon_threadsafe(self.nextfuture.set_result, (context, args))

    def throw(self, exception: BaseException):
        self.loop.call_soon_threadsafe(self.nextfuture.set_exception, exception)

    def stop(self):
        self.loop.call_soon_threadsafe(
            self.nextfuture.set_exception, QtStopIteration()
        )


class qt_to_async(QtCore.QObject, Generic[T, P]):
    cancelled: SignalProtocol[Any] = signal_builder(QtFuture) # type: ignore
    _called: SignalProtocol[
        Tuple[QtFuture[T], Tuple[object, ...], Dict[str, Any], contextvars.Context]
    ] = signal_builder(object)

    def __init__(
        self,
        coro: Callable[Concatenate[QtFuture[T], P], None],
        timeout: int | None = None,
        parent: QtCore.QObject | None = None,
    ):
        super().__init__(parent)
        assert not inspect.iscoroutinefunction(coro), (
            "This should not be a coroutine, but a normal qt slot"
        )
        self.coro = coro
        self.timeout = timeout
        self._called.connect(self.on_called)

    def on_called(
        self,
        typed_tuple: Tuple[
            QtFuture[T], Tuple[object, ...], Dict[str, Any], contextvars.Context
        ],
    ):
        qtfuture, args, kwargs, ctx = typed_tuple

        try:
            for ctx, value in ctx.items():
                ctx.set(value)

            self.coro(qtfuture, *args, **kwargs)  # type: ignore

        except Exception as e:
            logger.error(
                f"Error in Qt Coro even before assignemd {self.coro}", exc_info=True
            )
            qtfuture.reject(e)

    async def acall(self, *args: P.args, **kwargs: P.kwargs) -> T:
        qtfuture: QtFuture[T] = QtFuture()
        ctx = contextvars.copy_context()
        self._called.emit((qtfuture, args, kwargs, ctx))
        self.coro(qtfuture, *args, **kwargs)
        try:
            if self.timeout:
                context, x = await asyncio.wait_for(
                    qtfuture.aiofuture, timeout=self.timeout
                )
            else:
                context, x = await qtfuture.aiofuture

            for ctx, value in context.items():
                ctx.set(value)

            return x

        except asyncio.CancelledError:
            qtfuture.set_cancelled()
            self.cancelled.emit(qtfuture)
            raise


class qt_gen_to_async_gen(QtCore.QObject, Generic[T, P]):
    cancelled: SignalProtocol[Any] = signal_builder(QtFuture)
    _called: SignalProtocol[
        Tuple[QtGenerator[T], Tuple[object, ...], Dict[str, Any], contextvars.Context]
    ] = signal_builder(QtFuture, tuple, dict, object)

    def __init__(
        self,
        coro: Callable[Concatenate[QtGenerator[T], P], None],
        timeout: int | None = None,
        parent: QtCore.QObject | None = None,
    ):
        super().__init__(parent)
        assert not inspect.iscoroutinefunction(coro), (
            "This should not be a coroutine, but a normal qt slot"
        )
        self.coro = coro
        self.timeout = timeout
        self._called.connect(self.on_called)

    def on_called(
        self,
        typed_tuple: Tuple[
            QtGenerator[T], Tuple[object, ...], Dict[str, Any], contextvars.Context
        ],
    ):
        qtgenerator, args, kwargs, ctx = typed_tuple

        try:
            for ctx, value in ctx.items():
                ctx.set(value)

            self.coro(qtgenerator, *args, **kwargs)  # type: ignore

        except Exception as e:
            logger.error(
                f"Error in Qt Coro even before assignemd {self.coro}", exc_info=True
            )
            qtgenerator.throw(e)

    async def acall(self, *args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[T, None]:
        qtgenerator: QtGenerator[T] = QtGenerator()
        ctx = contextvars.copy_context()
        self._called.emit((qtgenerator, args, kwargs, ctx))

        try:
            while True:
                if self.timeout:
                    context, result = await asyncio.wait_for(
                        qtgenerator.nextfuture, timeout=self.timeout
                    )
                else:
                    context, result = await qtgenerator.nextfuture

                for ctx, value in context.items():
                    ctx.set(value)

                yield result

        except asyncio.CancelledError:
            qtgenerator.set_cancelled()
            self.cancelled.emit(qtgenerator)
            raise





class async_to_qt(QtCore.QObject, Generic[T, P]):
    errored: "SignalProtocol[BaseException]" = signal_builder(BaseException)  # type: ignore
    cancelled: "SignalProtocol[CancelledError]" = signal_builder(CancelledError)  # type: ignore
    returned: SignalProtocol[T] =  signal_builder(object)  # type: ignore
    
    
    _returnedwithoutcontext: "SignalProtocol[Tuple[T, contextvars.Context]]" =  signal_builder(object)  # type: ignore
    
 

    def __init__(
        self,
        function: Callable[P, Awaitable[T]],
        parent: QtCore.QObject | None = None,
    ):
        super().__init__(parent=parent)
        self.function = function
        self._returnedwithoutcontext.connect(self.on_returnedwithoutcontext)
        
    def on_returnedwithoutcontext(self, answer: Tuple[T, contextvars.Context]):
        res, ctxs = answer
        
        for ctx, value in ctxs.items():
            ctx.set(value)
        self.returned.emit(res)

    def run(self, *args: P.args, **kwargs: P.kwargs) -> KoilFuture[T]:
        
        
        koil_loop = get_koiled_loop_or_raise()

        my_signals = QtSignalHolder(
            returned=self._returnedwithoutcontext,
            cancelled=self.cancelled,
            errored=self.errored,
        )
        
        
        return run_async_sharing_context(
            self.function,
            koil_loop,
            my_signals,
            *args,
            **kwargs,
        )

            
    def __call__(self, *args: Any, **kwds: Any) -> Any:
        return self.run(*args, **kwds)


SendType = TypeVar("SendType")








class async_gen_to_qt(QtCore.QObject, Generic[T, P]):
    errored: SignalProtocol[BaseException] = signal_builder(Exception)
    cancelled: SignalProtocol[CancelledError] = signal_builder(Exception)
    yielded: SignalProtocol[T] = signal_builder(object)
    done: SignalProtocol[None] = signal_builder(object)
    _yieldedwithoutcontext: SignalProtocol[Tuple[T, contextvars.Context]] = signal_builder(object)

    def __init__(
        self,
        function: Callable[P, AsyncIterator[T]],
        unique: bool = False,
        parent: QtCore.QObject | None = None,
    ):
        super().__init__(parent=parent)
        self.function = function
        self._yieldedwithoutcontext.connect(self.on_yieldedwithoutcontext)

    def on_yieldedwithoutcontext(self, answer: Tuple[T, contextvars.Context]):
        res, ctxs = answer
        
        
        for ctx, value in ctxs.items():
            ctx.set(value)

        self.yielded.emit(res)


    def run(self, *args: P.args, **kwargs: P.kwargs) -> KoilFuture[None]:
        koil_loop = get_koiled_loop_or_raise()

        my_signals = QtIteratorSignalHolder(
            done=self.done,
            cancelled=self.cancelled,
            errored=self.errored,
            next=self._yieldedwithoutcontext,
        )

        return iterate_async_sharing_context(
            self.function,
            koil_loop,
            my_signals,
            *args,
            **kwargs,
        )
        
        



class WrappedObject(QtCore.QObject):
    def __init__(self, koil: Koil, parent: QtCore.QObject | None = None):
        super().__init__(parent=parent)
        self.koil = koil
        self._hooked_close_event = self.parent().closeEvent # type: ignore
        self.parent().closeEvent = self.on_close_event # type: ignore
        self.parent().destroyed.connect(self.destroyedEvent)

    def on_close_event(self, event: QtCore.QEvent) -> None: 
        """Hook the close event to exit the Koil loop"""
        if self.koil.running:
            self.koil.__exit__(None, None, None)
        self._hooked_close_event(event) # type: ignore

    def destroyedEvent(self):
        if hasattr(self, "koil"):
            if self.koil.running:
                self.koil.__exit__(None, None, None)


class QtKoil(Koil):
    def __init__(self, parent: QtCore.QObject):
        super().__init__()
        self.parent = parent
        self._qobject = None

    def __enter__(self):
        super().__enter__()
        assert self.parent, "Parent must be set before entering the loop"
        self._qobject = WrappedObject(parent=self.parent, koil=self)
        assert self._qobject.parent() is not None, (
            "No parent found. Please provide a parent"
        )
        ap_instance = QtWidgets.QApplication.instance()
        if ap_instance is None:
            raise NotImplementedError("Qt Application not found")
        return self

    class Config:
        arbitrary_types_allowed = True





class QtKoilMixin(QtWidgets.QWidget):
    """A mixin that allows you to use Koil in a Qt application.

    This mixin will automatically enter and exit the Koil loop when the
    application is started and stopped. It will also automatically set the
    Koil loop as the current loop for the application.

    """

    def __init__(self, *args, **kwargs): #type: ignore
        super().__init__(*args, **kwargs) # type: ignore
        self._koil = create_qt_koil(parent=self, auto_enter=False)

    @property
    def koil(self) -> QtKoil:
        return self._koil






def create_qt_koil(parent: QtCore.QObject, auto_enter: bool = True) -> QtKoil:
    koil = QtKoil(parent=parent)
    if auto_enter:
        koil.enter()
    return koil
