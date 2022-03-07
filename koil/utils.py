import asyncio
import contextvars
import concurrent.futures


def run_threaded_with_context(
    future: asyncio.Future, loop: asyncio.AbstractEventLoop
) -> concurrent.futures.Future:
    """Runs a futz in the supplied loop but copiesthe context
    of the current loop,

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future + newcontext
    """

    ctxs = contextvars.copy_context()

    async def passed_with_context(future):
        for ctx, value in ctxs.items():
            ctx.set(value)

        x = await future
        newcontext = contextvars.copy_context()
        return x, newcontext

    return asyncio.run_coroutine_threadsafe(passed_with_context(future), loop)
