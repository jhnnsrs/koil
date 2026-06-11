# koil

[![codecov](https://codecov.io/gh/jhnnsrs/koil/branch/main/graph/badge.svg?token=KXYAR09EEC)](https://codecov.io/gh/jhnnsrs/koil)
[![PyPI version](https://badge.fury.io/py/koil.svg)](https://pypi.org/project/koil/)
![Maintainer](https://img.shields.io/badge/maintainer-jhnnsrs-blue)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI status](https://img.shields.io/pypi/status/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI download day](https://img.shields.io/pypi/dm/koil.svg)](https://pypi.python.org/pypi/koil/)

**Predictable sync/async boundaries for Python — with cooperative cancellation, context propagation, and generator bridging.**

---

## What is koil?

koil is a library for calling async code from synchronous (and vice versa) Python in a structured, lifecycle-aware way. It runs a dedicated asyncio event loop on a background thread and provides a set of bridges that let synchronous code call coroutines, consume async generators, and run sync code back inside the loop — all with first-class support for cancellation and `ContextVar` propagation.

koil is *not* a general-purpose event-loop runner. It is designed for the specific problem of writing sync-facing APIs on top of async implementations, especially in long-lived applications (desktop apps, CLI tools, frameworks) where the event loop runs for the lifetime of the program and teardown must be predictable.

---

## Why not just use `asyncio.run`?

`asyncio.run` is perfect for scripts — one coroutine, runs to completion, loop closes. It breaks down when you need:

- A loop that lives across multiple calls (e.g. a context manager that holds a connection open).
- Async generators consumed as sync `for` loops.
- Cancellation of ongoing work when the caller (or the outer context) goes away.
- `ContextVar` values set in sync code to be visible inside the async coroutine and vice versa.
- Running sync-blocking code back from inside the async loop without deadlocking.

---

## How koil compares to similar libraries

| | koil | asgiref | qasync | nest_asyncio |
|---|---|---|---|---|
| **Primary direction** | both directions (main logic should be async) | both directions | sync → async (Qt loop *is* the asyncio loop) | patches nesting into existing loops |
| **Loop lifecycle** | Managed: starts and stops with a context manager | Caller-managed | Qt manages it | No management |
| **Cooperative cancellation** | Yes — propagates across the thread boundary | No | Depends on Qt signal delivery | No |
| **ContextVar propagation** | Yes — both directions | Partial | No | No |
| **Async generator → sync generator** | Yes — `unkoil_gen` | No | No | No |
| **Sync generator → async generator** | Yes — `iterate_threaded` | No | No | No |
| **Qt integration** | Yes — without replacing the Qt event loop | No | Yes — replaces the Qt event loop | No |
| **Structured teardown** | Yes — `__exit__` cancels tasks, joins thread | No | No | No |

### asgiref

[asgiref](https://github.com/django/asgiref) (`sync_to_async` / `async_to_sync`) is Django's bridge for handling sync views in an async server or async ORM calls from sync views. It solves a different problem: adapting individual callables across the boundary inside an already-running loop (the ASGI server's). It does not manage loop lifecycle, does not propagate cancellation to background threads, and does not bridge generators. If you are building a Django application, asgiref is the right tool. If you are building a long-lived desktop application or CLI that needs a persistent event loop with proper teardown, koil is a better fit.

### qasync

[qasync](https://github.com/CabbageDevelopment/qasync) makes the Qt event loop *be* the asyncio event loop. This is a good choice if you are writing a pure-async Qt application from scratch. koil takes the opposite approach: the asyncio loop runs on a separate thread, and Qt signals/slots are used as the communication channel between the loop and the Qt main thread. This means existing sync Qt code can call into the async loop without being rewritten, and the Qt main thread is never blocked by asyncio internals. It also means cancellation of a Qt-triggered async task propagates cleanly without Qt needing to know about asyncio task state.

### nest_asyncio

[nest_asyncio](https://github.com/erdewit/nest_asyncio) patches the running loop to allow nested `asyncio.run` calls. This can get scripts and notebooks out of trouble quickly but is not safe for production: it mutates global asyncio state and can cause subtle re-entrancy bugs under concurrent use.

---

## Installation

```bash
pip install koil
```

For Qt support:

```bash
pip install koil[qtpy]
```

---

## Core concepts

### The Koil context manager

`Koil` starts a background event loop and registers it as the ambient loop for the current thread. All bridging functions (`unkoil`, `run_threaded`, etc.) use this loop. Exiting the context manager cancels any remaining tasks, waits for the background thread to finish, and closes the loop.

```python
from koil import Koil, unkoil

async def fetch(url: str) -> str:
    ...  # real async work

with Koil():
    result = unkoil(fetch, "https://example.com")
```

### Configuring `Koil`

All options have sensible defaults; you only pass what you want to change:

```python
with Koil(
    sync_in_async=True,        # allow koil's sync bridges inside a running loop
    uvify=True,                # use uvloop for the background loop if installed
    shutdown_join_timeout=None,  # extra grace on teardown (None = 5.0s default)
    rewrite_tracebacks=True,   # hide koil-internal frames in user tracebacks
    cancel_timeout=2.0,        # grace for workers to acknowledge cancellation
):
    ...
```

| Option | Default | What it does |
|---|---|---|
| `sync_in_async` | `True` | Permits entering `Koil` and calling `unkoil` from a thread that already runs an asyncio loop (e.g. Jupyter notebooks). With `False`, doing so raises `ContextError` / `KoilError` instead — useful to catch accidental sync-in-async usage in pure-async applications. |
| `uvify` | `True` | Use [uvloop](https://github.com/MagicStack/uvloop) for the background event loop when it is installed, falling back to the stdlib loop otherwise. Set to `False` to force the stdlib loop (on Windows this selects a `SelectorEventLoop`). |
| `shutdown_join_timeout` | `None` | Extra seconds `__exit__` waits for the loop thread to stop *after* the initial graceful `cancel_timeout` wait, before abandoning it (the thread is a daemon, so the interpreter can still exit). `None` uses the module default `koil.loop.SHUTDOWN_JOIN_TIMEOUT` (5.0s). |
| `rewrite_tracebacks` | `True` | Prune koil's internal machinery frames from the tracebacks of exceptions that cross a bridge, keeping exactly one koil frame as a marker (see below). |
| `cancel_timeout` | `2.0` | How long, in seconds, koil waits for a cancelled worker to acknowledge cancellation: when a `run_threaded` task is cancelled, when a `*_with_timeout` deadline expires, and as the initial graceful wait in `__exit__`. Also settable as a plain attribute after construction. |

One knob lives as a module attribute rather than a constructor argument:

- `koil.utils.RESULT_POLL_INTERVAL` (default `0.05`) — how often a blocking `result()`/`unkoil` call wakes up so a pending Ctrl+C is delivered promptly. Smaller values make Ctrl+C more responsive at the cost of slightly more idle wakeups. It adds no latency to fast tasks. It is process-wide (a signal-delivery concern, not a per-loop one); override per call via `result(poll_interval=...)`.

### Traceback rewriting

When an exception raised in your code crosses a koil bridge, its traceback would normally include half a dozen frames of koil machinery and `concurrent.futures` glue. By default koil rewrites the traceback so you see your sync call site, **one** koil frame marking the bridge crossing, and then your failing async code:

```
Traceback (most recent call last):
  File "app.py", line 14, in <module>
    unkoil(load_profile)
  File ".../koil/bridge.py", line 261, in unkoil
    return context_aware_future.result()  # <- crossed the koil bridge
  File "app.py", line 9, in load_profile
    return await fetch_user(42)
  File "app.py", line 6, in fetch_user
    raise ValueError(f"no such user: {uid}")
ValueError: no such user: 42
```

Exceptions raised by koil itself (e.g. a cancellation timeout) keep their full traceback — there the koil frames *are* the informative part. To get full tracebacks for everything:

- per instance: `Koil(rewrite_tracebacks=False)`;
- process-wide, without touching code: set the environment variable `KOIL_FULL_TRACEBACK=1` (this wins over everything — the escape hatch when debugging koil itself).

### One thread, many calls

The background loop thread is created **once** when you enter the `Koil` context. Every subsequent `unkoil`, `unkoil_gen`, or `unkoil_task` call posts a coroutine to that existing thread via `asyncio.run_coroutine_threadsafe` — no new threads are spawned per call. The calling thread blocks on a `concurrent.futures.Future` until the result arrives; the loop thread continues processing other tasks in the meantime.

This matters in practice. Calling `unkoil` a thousand times inside a `with Koil():` block creates one thread total, not a thousand. Multiple `@koilable` objects entered inside the same `Koil` context all share that single loop thread too.

The only functions that touch the thread pool are `run_threaded` and `iterate_threaded`, and only because they genuinely need to run blocking sync code without stalling the loop. Even then, they reuse Python's default `ThreadPoolExecutor` — no new thread is started if a pool thread is available.

This is in contrast to libraries like asgiref's `async_to_sync`, which creates (or reuses per-thread) a fresh event loop for each blocking call site, or frameworks that spin up a new executor thread per bridged call. koil's model scales to high call frequencies with minimal threading overhead.

---

## Bridging async → sync

### `unkoil` — call a coroutine, block until done

```python
from koil import Koil, unkoil

async def compute(x: int) -> int:
    await asyncio.sleep(0.1)
    return x * 2

with Koil():
    result = unkoil(compute, 21)   # 42
```

### `unkoil_gen` — consume an async generator as a sync `for` loop

```python
from koil import Koil, unkoil_gen

async def stream():
    for i in range(5):
        await asyncio.sleep(0.1)
        yield i

with Koil():
    for value in unkoil_gen(stream):
        print(value)   # 0 1 2 3 4
```

### `unkoil_task` — fire and forget, get a future back

```python
from koil import Koil, unkoil_task

with Koil():
    future = unkoil_task(compute, 21)
    # do other work
    result = future.result()   # blocks until done; future.cancel() signals cancellation
```

---

## Timeouts

Every blocking bridge has a `*_with_timeout` variant that bounds the call and
raises `KoilTimeoutError` on expiry. They are separate functions (not a
`timeout=` keyword) so the timeout can never collide with a `timeout`
argument of *your* function — your `*args`/`**kwargs` are forwarded untouched:

```python
from koil import (
    Koil,
    KoilTimeoutError,
    unkoil_with_timeout,
    unkoil_task_with_timeout,
    unkoil_gen_with_timeout,
)

with Koil():
    try:
        result = unkoil_with_timeout(fetch, 5.0, "https://example.com")
    except KoilTimeoutError:
        ...  # fetch was cancelled on the loop and did not leak
```

What happens on expiry: the deadline is enforced **on the koil loop** — the
coroutine is cancelled, koil waits up to `cancel_timeout` for it to
acknowledge, and only then raises. A timed-out task is never silently
orphaned. `KoilTimeoutError` subclasses both `KoilError` and the builtin
`TimeoutError`, so a plain `except TimeoutError` catches it.

- `unkoil_with_timeout(fn, timeout, *args, **kwargs)` — bounded `unkoil`.
- `unkoil_task_with_timeout(fn, timeout, *args, **kwargs)` — the deadline is
  enforced even if you never call `result()`; a fire-and-forget task is still
  cancelled when it expires.
- `unkoil_gen_with_timeout(fn, timeout, *args, **kwargs)` — a **per-step**
  inactivity bound: each iteration step must produce its value within
  `timeout` seconds, but the generator may run arbitrarily long overall as
  long as it keeps yielding.
- `future.result(timeout=...)` — a wait bound at the call site of an existing
  `unkoil_task` future (mirrors `concurrent.futures.Future.result`). On
  expiry the task is cancelled cooperatively before `KoilTimeoutError` is
  raised.
- Qt: `async_to_qt(fn, timeout=...)` and `async_gen_to_qt(fn, timeout=...)`
  take the timeout at construction; expiry emits the `errored` signal with a
  `KoilTimeoutError` (the `cancelled` signal does not fire). For the
  generator wrapper the bound is the **total** drain time, since it runs as
  one loop task.

---

## Bridging sync → async

When async code needs to call back into sync-blocking work (e.g. a CPU-bound function, a blocking library), koil provides `run_threaded` and `iterate_threaded`. These run the sync code on a thread-pool executor while keeping the async loop responsive.

### `run_threaded` — await a sync function from inside async code

```python
from koil import Koil, unkoil, sleep
from koil.bridge import run_threaded
import time

def slow_computation(n: int) -> int:
    sleep(1)
    return n * 2

async def pipeline(n: int) -> int:
    result = await run_threaded(slow_computation, n)
    return result

with Koil():
    print(unkoil(pipeline, 21))   # 42, loop stayed responsive during the sleep
```

### `iterate_threaded` — consume a sync generator from inside async code

```python
from koil.bridge import iterate_threaded

def blocking_source(n: int):
    for i in range(n):
        sleep(0.1)
        yield i

async def consume():
    async for value in iterate_threaded(blocking_source, 5):
        print(value)
```

---

## Cancellation

Cancellation is the feature that most async/sync bridges get wrong. koil treats it as a first-class concern.

### Cancelling from the async side (`run_threaded`)

If the asyncio task awaiting `run_threaded` is cancelled, koil immediately sets a thread-safe cancel event on the worker thread. The loop then waits up to `Koil.cancel_timeout` seconds for the worker to finish. The worker can check for cancellation cooperatively:

```python
from koil import check_cancelled, sleep
from koil.bridge import run_threaded

def long_job(n: int) -> int:
    for i in range(n):
        check_cancelled()   # raises ThreadCancelledError if cancelled, any unkoil call here has it implicitly
        sleep(0.1) # also koil sleeps are cancellation points, 
        # time.sleep(0.1) would work too but would not be interruptible until the sleep finishes (don't use it)
    return n

async def run():
    task = asyncio.create_task(run_threaded(long_job, 100))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass   # long_job was interrupted cleanly
```

### Cancelling from the sync side (`unkoil_task`)

```python
from koil import Koil, unkoil_task

with Koil():
    future = unkoil_task(some_long_coroutine)
    # later:
    future.cancel()   # sets the cancel event on the koil loop
```

### Cancellation-aware sleep

`koil.sleep` is a drop-in replacement for `time.sleep` inside koil worker threads. It respects cancellation and does not block the event loop:

```python
from koil import sleep, check_cancelled

def worker():
    for _ in range(10):
        check_cancelled()
        sleep(1.0)   # cooperative, cancelled immediately if the task is cancelled
```

---

## Context variable propagation

`ContextVar` values are copied from the calling context into the coroutine and back again, in both bridge directions. Code inside the async loop sees the same context as the sync caller, and any changes made inside the coroutine are visible to the caller after `unkoil` returns.

```python
from contextvars import ContextVar
from koil import Koil, unkoil

request_id: ContextVar[str] = ContextVar("request_id")

async def handler() -> str:
    return request_id.get()   # sees the value set by the sync caller

with Koil():
    request_id.set("req-123")
    print(unkoil(handler))   # "req-123"
```

---

## The `@koiled` decorator — dual sync/async functions

`@koiled` makes a single `async def` callable from both worlds: in an async
context a call returns the awaitable as usual; in a sync context (inside a
`with Koil():` block) the call runs on the koil loop and blocks for the
result. Async generators work too (sync callers get a regular generator).

```python
from koil import Koil, koiled
import asyncio

@koiled
async def fetch(url: str) -> str:
    await asyncio.sleep(0.1)
    return f"<{url}>"

@koiled
async def stream(n: int):
    for i in range(n):
        await asyncio.sleep(0.01)
        yield i

# Sync caller
with Koil():
    print(fetch("https://example.com"))    # blocks, returns the str
    for item in stream(3):
        print(item)

# Async caller — same functions
async def main():
    print(await fetch("https://example.com"))
    async for item in stream(3):
        print(item)
```

`@koiled(timeout=5)` bounds sync calls via `unkoil_with_timeout`. For static
typing, the wrapper is typed from the sync caller's point of view; async
callers can use the fully-typed original via `await fetch.aio(...)`.

## `koiled_cm` — wrap an async context manager instance

When you don't own the class (e.g. `httpx.AsyncClient`), wrap an instance:

```python
from koil import koiled_cm

with koiled_cm(httpx.AsyncClient()) as client:
    ...  # __aenter__/__aexit__ ran on the koil loop
```

If no koil context is active, a `Koil` is started on enter and torn down on
exit (also when `__aenter__` raises).

## The `@koilable` decorator

`@koilable` generates `__enter__` / `__exit__` for any class that implements `__aenter__` / `__aexit__`. It starts a `Koil` automatically if none is active, making async context managers transparently usable in sync code. It works bare (`@koilable`) or with arguments (`@koilable(add_connectors=True)`).

```python
from koil import koilable, koiled
import asyncio

@koilable
class DataStream:
    async def __aenter__(self):
        await asyncio.sleep(0)   # connect
        return self

    async def __aexit__(self, *args):
        await asyncio.sleep(0)   # disconnect

    @koiled
    async def fetch(self) -> int:
        await asyncio.sleep(0.01)
        return 42

    @koiled
    async def stream(self):
        for i in range(5):
            await asyncio.sleep(0.01)
            yield i


# Sync usage — no asyncio knowledge required
with DataStream() as ds:
    print(ds.fetch())
    for item in ds.stream():
        print(item)
```

### `KoiledModel` — Pydantic integration

```python
from koil.composition import KoiledModel

class MyService(KoiledModel):
    url: str

    async def __aenter__(self):
        # setup
        return self

    async def __aexit__(self, *args):
        # teardown
        pass

with MyService(url="http://example.com") as svc:
    ...
```

---

## Qt integration

koil's Qt integration runs the asyncio loop on a background thread and uses Qt signals as the bridge — the Qt event loop is never blocked or replaced.

```python
from koil.qt import async_to_qt, qt_to_async, QtFuture, create_qt_koil
from qtpy import QtWidgets

class MyWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self._koil = create_qt_koil(parent=self)

        # Wrap an async function so it can be called from a Qt slot
        self.runner = async_to_qt(self.my_coroutine)
        self.runner.returned.connect(self.on_result)
        self.runner.errored.connect(self.on_error)

        button = QtWidgets.QPushButton("Run")
        button.clicked.connect(lambda: self.runner.run())
        ...

    async def my_coroutine(self):
        await asyncio.sleep(1)
        return 42

    def on_result(self, value):
        print(f"Got {value} on the Qt main thread")

    def on_error(self, exc):
        print(f"Error: {exc}")
```

`qt_to_async` goes the other direction: it wraps a Qt slot so it can be awaited from inside the async loop, with the slot executing on the Qt main thread and resolving a `QtFuture` when done.

---

## Deprecated import paths

The legacy shim modules `koil.koil`, `koil.helpers`, and `koil.vars` now emit
a `DeprecationWarning` on attribute access and will be removed in a future
major release — import from `koil.loop`, `koil.bridge`, and `koil.context`
(or just `koil`) instead. The old `run_spawned` / `iterate_spawned` aliases
are deprecated names for `run_threaded` / `iterate_threaded`.

---

## When to use koil

Use koil when:

- You are writing a **sync-facing API** on top of an async implementation (e.g. a library that works both ways).
- You need a **persistent event loop** that lives for the duration of a context manager, not just a single call.
- You need **cancellation to propagate** cleanly across the thread boundary in both directions.
- You need **`ContextVar` values to flow** between sync and async code.
- You are working with **Qt** and do not want to replace or patch the Qt event loop.
- You want to consume **async generators as sync `for` loops** or drive **sync generators from async code**.

Do not use koil when:

- You are writing a pure async application — use asyncio directly.
- You are inside a Django/ASGI server — use asgiref.
- You only need to run a single coroutine to completion once — use `asyncio.run`.
