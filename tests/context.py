import asyncio
from types import TracebackType
from typing import Self
from koil.decorators import koilable
import contextvars


t = contextvars.ContextVar("t", default=0)


@koilable()
class AsyncContextManager:
    def __init__(self) -> None:
        pass

    async def aprint(self):
        await asyncio.sleep(0.01)
        return "sss"

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass
    
    def __enter__(self) -> Self:
        ...
        
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        ...
