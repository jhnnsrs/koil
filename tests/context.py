import asyncio
import contextvars
from types import TracebackType

from koil.decorators import koilable

t = contextvars.ContextVar("t", default=0)


@koilable()
class AsyncContextManager:
    def __init__(self) -> None:
        pass

    async def aprint(self) -> str:
        await asyncio.sleep(0.01)
        return "sss"

    async def __aenter__(self) -> "AsyncContextManager":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    def __enter__(self) -> "AsyncContextManager": ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...
