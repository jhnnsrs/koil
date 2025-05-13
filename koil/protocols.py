
from typing import (
    Callable,
    Generic,
    Tuple,
    TypeVar,
    Protocol,
)
import contextvars
from .errors import CancelledError

T = TypeVar("T")

class SignalProtocol(Protocol, Generic[T]):
    def connect(self, slot: Callable[[T], None]) -> None:
        """Connect a slot to the signal.

        Args:
            slot (Callable[[T], None]): The slot to connect.
        """
        ...

    def emit(self, *args: T) -> None:
        """Emit the signal with the given arguments.

        Args:
            *args (T): The arguments to emit.
        """
        ...
        
        
        
class TaskSignalProtocol(Protocol, Generic[T]):
    """Protocol for task signals."""

    @property
    def cancelled(self) -> SignalProtocol[CancelledError]:
        """Signal that the task was cancelled."""
        ...
    
    @property  
    def returned(self) -> SignalProtocol[Tuple[T, contextvars.Context]]:
        """Signal that the task was finished with a result."""
        ...
    
    @property   
    def errored(self) -> SignalProtocol[BaseException]:
        """Signal that the task was finished with an exception."""
        ...
        
        
class IteratorSignalProtocol(Protocol, Generic[T]):
    """Protocol for iterator signals."""

    @property
    def cancelled(self) -> SignalProtocol[CancelledError]:
        """Signal that the iterator was cancelled."""
        ...
    
    @property  
    def next(self) -> SignalProtocol[Tuple[T, contextvars.Context]]:
        """Signal that the iterator was finished with a result."""
        ...
        
    @property
    def done(self) -> SignalProtocol[None]:
        """Signal that the iterator was finished."""
        ...
    
    @property   
    def errored(self) -> SignalProtocol[BaseException]:
        """Signal that the iterator was finished with an exception."""
        ...