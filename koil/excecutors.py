import contextvars
from concurrent.futures import ThreadPoolExecutor

default_executor = contextvars.ContextVar("default_executor", default=ThreadPoolExecutor())


class Engine:
    executorClass = ThreadPoolExecutor

    def __init__(self, *args, **kwargs) -> None:
        self.executor = self.executorClass(*args,**kwargs)

    def __enter__(self):
        default_executor.set(self.executor)
        self.executor.__enter__()
        return self

    def __exit__(self, *args, **kwargs):
        self.executor.__exit__(*args, **kwargs)
        default_executor.set(None)

    
    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *args, **kwargs):
        return self.__exit__(*args, **kwargs)
        

class ThreadEngine(Engine):
    executorClass = ThreadPoolExecutor



