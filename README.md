# koil

[![codecov](https://codecov.io/gh/jhnnsrs/koil/branch/master/graph/badge.svg?token=UGXEA2THBV)](https://codecov.io/gh/jhnnsrs/koil)
[![PyPI version](https://badge.fury.io/py/koil.svg)](https://pypi.org/project/koil/)
![Maintainer](https://img.shields.io/badge/maintainer-jhnnsrs-blue)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI status](https://img.shields.io/pypi/status/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI download day](https://img.shields.io/pypi/dm/koil.svg)](https://pypi.python.org/pypi/koil/)


### Inspiration

koil is an abstraction layer for threaded asyncio to enable "sensible defaults" for
programmers working with frameworks that are barely compatible with asyncio (originally developped to get around pyqt5)

### Installation

```bash
pip install koil
```


### Main Concept

Async libraries are amazing, and its an ecosystem rapidly increasing, however in some contexts it still doesn't seem like
the way to go and the burden of learning these concepts might be to high. However you developed a wonderful async api
that you want to share with the world. Of couse because you care about cleaning up your resources you made it a context.

```python


class AmazingAsyncAPI:
    def __init__(self) -> None:
        pass

    async def sleep(self):
        await asyncio.sleep(0.01)
        return "the-glory-of-async"

    async def yielding_sleeper(self):
        for i in range(0, 20):
            await asyncio.sleep(0.01)
            yield i

    async def __aenter__(self):
        # amazing connection logic
        return self

    async def __aexit__(self, *args, **kwargs):
        # amazing tear down logic
        return self

```

However if somebody wants to use this api in sync environment they are in for a good one, as a call to asyncio.run() just won't do the trick.
And you will have to write a lot of boilerplate code to make it work. And once you start trying to use the yielding_sleeper
you will be in for a good one.

```python

async def the_annoying_wrapper():

    async with AmazingAsyncAPI() as e:
        print(await e.sleep()) # easy enough

        # How do I use the output of the yielding
        # sleeper? Queues? Another thread?


asyncio.run(the_annoying_wrapper())

```

Well koil is here to help. Just mark your class with koilable and the functions that you want to be able to call from
a sync context with unkoilable.


```python
from koil import koilable, unkoilable

@koilable
class AmazingAsyncAPI:
    def __init__(self) -> None:
        pass

    @unkoilable
    async def sleep(self):
        await asyncio.sleep(0.01)
        return "the-glory-of-async"

    @unkoilable
    async def yielding_sleeper(self):
        for i in range(0, 20):
            await asyncio.sleep(0.01)
            yield i

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

  for i in e.yielding_sleeper():
    print(i)

# Context manager is closed and cleaned up
```>


## How does it work?

Koil under the hood spawns a new event loop in another thread, calls functions that are marked with unkoilable
threadsafe in that loop and returns the result, when exiting it shuts down the loop in the other thread.

### Other usages

If you have multiple context managers or tasks that you would just like to run in another thread, we do *not* 
spawn a new thread for each of them, but rather use the same thread for all of them. This is to avoid the overhead
of spawning a new thread for each context manager. On the asyncio side, all tasks will be in the same loop, so
you can use asyncio primitives to communicate between them.

You can also just use Koil as a threaded event loop, and use the unkoil function to run functions in that loop.

```python

async def task(arg):
       x = await ...
       return x

with Koil(): # creates a threaded loop

    x = unkoil(task, 1)

    with AmazingAsyncAPI as e:
       print(e.sleep())

```

Importantly, Koil also provides primites to run sync functions (in another thread, and experimentally in another process)
and await them in the koil loop. This is useful if you have a sync function that you want to use in an async context.

```python
from koil.helpers import run_spawned, iterate_spawned
import time

def sync_function(arg):
    return arg

def sync_generator(arg):
    for i in range(0, arg):
        time.sleep(1)
        yield i

async def run_async():
    x = await run_spawned(sync_function, 1)

    async for i in iterate_spawned(sync_generator, 1):
        print(i)

    return x

with Koil():
    x = unkoil(run_async)

```

THis allows you to use async primitives to communicate with sync functions. Note that this is not a good idea
if you have a lot of sync functions, as the overhead of spawning a new thread for each of them is quite high.
You can however pass a threadpool to the run_spawned function to avoid this overhead.


## Task Support

Sometimes you want to run a task in the background and just get the result when you need it. Can't Koil do that?
Well it could, but we belive this is a bad idea. You should have that in the async world. However you can
browser our code and use our deprecated functions to do that. We just don't think its a good idea.

## PyQt Support

One of the main reasons for koil was to get around the fact that PyQt5 is not asyncio compatible, and all the### Installation

```bash
pip install koil
```


```python
from koil.qt import create_qt_koil, koilqt, unkoilqt

class KoiledInterferingFutureWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = create_qt_koil(parent=self) 
        # important to set parent, helps with cleanup
        # once the widget is destroyed. Loop starts automatically

        self.ado_me = koilqt(self.in_qt_task, autoresolve=True)
        # We can make qt functions callable from the async loop

        self.loop_runner = unkoilqt(self.ado_stuff_in_loop)
        self.loop_runner.returned.connect(self.task_finished)

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.greet_label = QtWidgets.QLabel("")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.call_task_button)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.call_task_button.clicked.connect(self.my_coro_task.run)

    def in_qt_task(self, future: QtFuture):
        """This is a task that is run in the Qt Main Thread"""
        # We can do stuff in the Qt Thread
        # We can resolve the future at any time
        # self.on_going_future = future

        future.resolve("called")

    def task_finished(self):
        """This is called when the task is finished. Main thread"""
        self.greet_label.setText("Hello!")

    async def ado_stuff_in_loop(self):
        """This is a coroutine that is run in the threaded loop"""
        x = await self.ado_me()
        self.coroutine_was_run = True


def main():
    app = QtWidgets.QApplication(sys.argv)
    widget = KoiledInterferingFutureWidget()
    widget.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()

```




