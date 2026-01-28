---
sidebar_position: 3
---

# Basic Usage

## Running Async Functions Synchronously

The `unkoil` function allows you to call an async function from a synchronous context. It handles the communication with the background event loop.

```python
import asyncio
from koil import Koil, unkoil

async def async_sleep(seconds):
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

# Initialize the Koil loop (usually done once at the start of your program)
with Koil():
    # Call the async function synchronously
    result = unkoil(async_sleep, 1)
    print(result)
```

## Iterating Async Generators

`unkoil_gen` allows you to iterate over an asynchronous generator as if it were a standard synchronous generator.

```python
from koil import Koil, unkoil_gen
import asyncio

async def async_counter(limit):
    for i in range(limit):
        await asyncio.sleep(0.1)
        yield i

with Koil():
    # Iterate synchronously
    for number in unkoil_gen(async_counter, 5):
        print(number)
```

## The `@koilable` Decorator

This is the most powerful feature for library authors. By decorating a class with `@koilable`, you enable it to handle its own lifecycle in both sync and async contexts.

The class must implement `__aenter__` and `__aexit__`. The decorator adds `__enter__` and `__exit__` methods that automatically spin up a `Koil` loop if one isn't running.

```python
from koil import koilable, unkoil
import asyncio

@koilable
class MyAsyncService:
    def __init__(self):
        self.connected = False

    async def __aenter__(self):
        await asyncio.sleep(0.1) # Simulate connection
        self.connected = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await asyncio.sleep(0.1) # Simulate teardown
        self.connected = False

    async def afetch_data(self):
        if not self.connected:
            raise RuntimeError("Not connected")
        await asyncio.sleep(0.1)
        return {"data": 123}

    def fetch_data(self):
        # Expose a sync version using unkoil
        return unkoil(self.afetch_data)

# Usage in Async Context
async def async_main():
    async with MyAsyncService() as service:
        print(await service.afetch_data())

# Usage in Sync Context (Koil handles the loop automatically)
def sync_main():
    with MyAsyncService() as service:
        print(service.fetch_data())
```
