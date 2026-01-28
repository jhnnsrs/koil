---
sidebar_position: 2
---

# Core Concepts

## The Problem
Python's `asyncio` is powerful, but mixing it with synchronous code (like legacy codebases or GUI frameworks) is often painful. You cannot simply call `asyncio.run()` inside a running event loop, and managing a separate thread for the loop requires boilerplate code.

## The Solution
`koil` manages a threaded event loop for you. It provides tools to:
1.  **Unkoil**: Run async functions synchronously by offloading them to the threaded loop and waiting for the result.
2.  **Koilable**: Decorate classes to make them usable in both `async with` and `with` contexts automatically.
3.  **Qt Integration**: Bridge asyncio tasks with Qt signals and slots.
