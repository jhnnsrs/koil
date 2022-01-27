# koil

[![codecov](https://codecov.io/gh/jhnnsrs/koil/branch/master/graph/badge.svg?token=UGXEA2THBV)](https://codecov.io/gh/jhnnsrs/koil)
[![PyPI version](https://badge.fury.io/py/koil.svg)](https://pypi.org/project/koil/)
![Maintainer](https://img.shields.io/badge/maintainer-jhnnsrs-blue)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI status](https://img.shields.io/pypi/status/koil.svg)](https://pypi.python.org/pypi/koil/)
[![PyPI download day](https://img.shields.io/pypi/dm/koil.svg)](https://pypi.python.org/pypi/koil/)

### DEVELOPMENT

### koil is asyncio for humans

koil is an abstraction layer on top of asyncio to enable "sensible defaults" for
programmers working with frameworks that are barely compatible with asyncio (originally developped to get around pyqt5)

### Why an abstraction layer on top of asyncio

asyncio is a magificent addition to the python framework and is facilitating asyncronous
and concurrent programming in a simple way.

However especially in scientific and datascience contexts the benefits of diving
into the concepts of asyncio might not outweigh the costs of learning these paradigms.

Additionally there are huge players in the python ecoystem that do not work seemlessly
with asyncio yet and involve workarounds (Django, PyQt). Koil tries to encapsulate
these workarounds in a simple utility library that wraps your api in a safe context
that can be used form this frameworks.

### Example

Asyncio Way

```python
import asyncio

async def call_api(sleep):
    await asyncio.sleep(4)
    return 5


print(asyncio.run(call_api(3)))

```

Koil Way

```python
import asyncio
from koil import koil

@koil
async def call_api(sleep):
    await asyncio.sleep(4)
    return 5

print(call_api(sleep)) # note the ommision of the asyncio.run call

```

On paper not a lot has changed between asyncio and koil, however under the hood the following
happened

When calling call_api for the first time, koil detected that we are not running in an asyncronous
event loop and therefore wrapped the call in asyncio.run.

This doesn't justify a new library and a perfomance decrease though. So lets see this scenario

```python
import asyncio
from koil import koil
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtWidgets import QApplication, QMainWindow, QPushButton

@koil
async def call_api(interval):
    await asyncio.sleep(interval)
    return 5


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("My App")
        button = QPushButton("Press Me!")
        button.clicked.connect(self.button_clicked)
        self.setCentralWidget(button)
        self.sleep_task = None

    def button_clicked(self):
        if self.sleep_task: self.sleep_task.cancel()
        self.sleep_task = call_api(interval, as_task=True)
        self.sleep_task.resolve.connect(lambda x: self.button.setText(f"Slept {x} seconds"))



app = QApplication(sys.argv)

window = MainWindow()
window.show()

app.exec()

```

Now this is more koils problem set. Koil detected on first run (when creating the Koil object) that
we are running in an QtApplication instance and loaded our asyncornous task off to another thread
to not interfer with our qt event loop. By passing as_task we also specified that we are do not
want to block the qt thread to wait for result in the other thread. Koil knowingly that we are in a Qt app
allows us to now connect to signals of that specific task in the ui_thread.

Here koil differnts itself of libraries like qasync that try to mimik the qt event loop in an asyncio event loop.
With qasync you adopt to the asyncio style and not the style you may want.

### Jupyter notebook

Jupyter is in a unique position as it itself runs in an event loop but is mainly used by scientists
that are used to an syncronus interface.

Your library

```python

@koil
async def call_api(interval):
    await asyncio.sleep(interval)
    return 5


```

By default koil makes the assumption that if not specified differently it will use a synconrous
interface in juypter so.

```python

call_api()

```

Would run your Api Task in another Thread and return the result synconrously back to you.
However you can opt in to use a asyncronous api.

```python
from koil import Koil

Koil(force_async=True) #at the top of your programm


await call_api()
```

Making the call run in the same event loop of jupyter.

### Considerations

Koil is a library that doesnt want to exist. In general it provides convenience method
to build asyncornous apis for a still synconrous world.

Therefore when adding koil to your api make sure to not ditch your asyncrs api but extend
them through convenience methods. One such trick would be to leave your asyncornous functions
untouched and extend them with koiled versions.

```python
import koil

class MyComplexApi:

    def __init__(self):
        ...

    async def acall_endpoint(self, endpoint): #a as convention for asyncornous functions
        ...


    def call_endpoint(self, *args, *kwargs):
        return koil(self.acall_endpoint)(*args, **kwargs)

```

Like this you are not experiencing any perfomance hints when running in a complete
asyncornous context, but you enable unfamiliar users to use your application in a
syncronous world if they so desire.

Also it is wise to always call your pure async functions from other async functions.

```python
import koil
import asyncio

class MyComplexApi:

    def __init__(self):
        self.connected = False
        ...

    async def aconnect(self):
        await asyncio.sleep(3)
        self.connected = True


    async def acall_endpoint(self, endpoint):
        if not self.connected:
            await self.aconnect() # call asyncronous no koiled version for better performance

        ...


    def call_endpoint(self, *args, *kwargs):
        return koil(self.acall_endpoint)(*args, **kwargs)

```
