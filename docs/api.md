---
sidebar_position: 6
---

# API Reference

## `koil.Koil`
The main context manager.
*   `__init__(sync_in_async=True, uvify=True)`:
    *   `sync_in_async`: If False, raises an error if you try to use Koil inside an existing asyncio loop.
    *   `uvify`: Tries to use `uvloop` if available for better performance.

## `koil.unkoil(coro, *args, **kwargs)`
Runs a coroutine synchronously in the Koil loop.

## `koil.unkoil_gen(async_gen, *args, **kwargs)`
Iterates over an async generator synchronously.

## `koil.koilable`
Decorator for classes.
*   Adds `__enter__` and `__exit__` methods.
*   Ensures a global `Koil` instance is available.

## `koil.qt`
*   `create_qt_koil(parent)`: Attaches a Koil loop to a Qt object.
*   `async_to_qt(func)`: Wraps an async function to return a `QtFuture` and emit signals.
*   `async_gen_to_qt(func)`: Wraps an async generator to emit signals for each yield.
