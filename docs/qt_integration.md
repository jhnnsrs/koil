---
sidebar_position: 4
---

# Qt Integration

`koil` was originally built to solve the problem of using asyncio with PyQt5. It provides a set of tools to run asyncio tasks without freezing the Qt GUI.

## Setup

In your main window or application setup, initialize `koil` with `create_qt_koil`.

```python
from PyQt5 import QtWidgets
from koil.qt import create_qt_koil

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        # Initialize Koil for Qt
        self.koil = create_qt_koil(self) 
```

## Wrapping Async Functions

Use `async_to_qt` (for functions) and `async_gen_to_qt` (for generators) to create wrappers that emit Qt signals.

```python
from PyQt5 import QtWidgets
from koil.qt import async_to_qt
import asyncio

async def heavy_task(seconds):
    await asyncio.sleep(seconds)
    return "Done"

class MyWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.button = QtWidgets.QPushButton("Start Task")
        self.label = QtWidgets.QLabel("Ready")
        
        # Create a Qt-compatible wrapper
        self.task_wrapper = async_to_qt(heavy_task)
        
        # Connect signals
        self.task_wrapper.returned.connect(self.on_success)
        self.task_wrapper.errored.connect(self.on_error)
        
        self.button.clicked.connect(self.start_task)

    def start_task(self):
        self.label.setText("Running...")
        # Run the task (non-blocking)
        self.task_wrapper.run(2)

    def on_success(self, result):
        self.label.setText(result)

    def on_error(self, error):
        self.label.setText(f"Error: {error}")
```
