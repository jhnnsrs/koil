import asyncio
from koil.decorators import koilable, unkoilable
import contextvars


t = contextvars.ContextVar("t", default=0)


@koilable()
class AsyncContextManager:
    def __init__(self) -> None:
        pass

    @unkoilable
    async def aprint(self):
        await asyncio.sleep(0.01)
        return "sss"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        return self
