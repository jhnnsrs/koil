# koil

[![codecov](https://codecov.io/gh/jhnnsrs/koil/branch/master/graph/badge.svg?token=UGXEA2THBV)](https://codecov.io/gh/jhnnsrs/koil)
[![PyPI version](https://badge.fury.io/py/koil.svg)](https://pypi.org/project/koil/)
![Maintainer](https://img.shields.io/badge/maintainer-jhnnsrs-blue)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI status](https://img.shields.io/pypi/status/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI download day](https://img.shields.io/pypi/dm/koil.svg)](https://pypi.python.org/pypi/koil/)

### DEVELOPMENT

# Quick Start

Let's discover **Koil in less than 5 minutes**.

### Inspiration

koil is an abstraction layer for threaded asyncio to enable "sensible defaults" for
programmers working with frameworks that are barely compatible with asyncio (originally developped to get around pyqt5)

### Main Concept

Async libraries are amazing, and its an ecosystem rapidly increasing, however in some contexts it still doesn't seem like
the way to go and the burden of learning these concepts might be to high. However you developed a wonderful async api
that you want to share with the world.

```python
class AmazingAsyncAPI:
    def __init__(self) -> None:
        pass

    async def sleep(self):
        await asyncio.sleep(0.01)
        return "the-glory-of-async"

    async def __aenter__(self):
        # amazing connection logic
        return self

    async def __aexit__(self, *args, **kwargs):
        # amazing tear down logic
        return self

```

However if somebody wants to use this api in sync environment they are in for a good one, as a call to asyncio.run() just wont do the trick.

```python
from koil import koilable, unkoilable

@koilable()
class AmazingAsyncAPI:
    def __init__(self) -> None:
        pass

    @unkoilable()
    async def sleep(self):
        await asyncio.sleep(0.01)
        return "the-glory-of-async"

    async def __aenter__(self):
        # amazing connection logic
        return self

    async def __aexit__(self, *args, **kwargs):
        # amazing tear down logic
        return self

```

And now it works. Just use your Api with a normal context manager.

```python
with AmazingAsyncAPI as e:
  print(e.sleep())
```

Koil under the hood spawns a new event loop in another thread, calls functions that are marked with unkoilable
threadsafe in that loop and returns the result, when exiting it shuts down the loop in the other thread.

If you have multiple context managers or tasks that you would just like to run in another thread, you can
also create a loop in another thread

```python

async def task(arg):
       x = await ...
       return x

with Koil(): # creates a threaded loop

    x = unkoil(task, 1)

    with AmazingAsyncAPI as e:
       print(e.sleep())

```

Moreover koil also is able to be used with generators

```python
import asyncio
from koil import unkoil_gen

async def task(arg):
    for i in range(0,20)
      await asyncio.sleep(1)
      yield arg


with Koil(): # creates a threaded loop

    for x in unkoil_gen(task, 1):
        print(x)

```

And finally koil is able to create task like objects,

```python
async def task(arg):
    await asyncio.sleep(2)
    return arg

with Koil():

  x = unkoil(task, 1, as_task=True)

  # do other stuff

  if x.done():
      print(x)

```

## PyQt Support

... Documentation coming soon...

### Installation

```bash
pip install koil
```
