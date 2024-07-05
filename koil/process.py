from .vars import output_queue_context, input_queue_context, in_process_context
import multiprocessing

from .errors import ProcessCancelledError, KoilError
from concurrent.futures import ThreadPoolExecutor
import asyncio


try:
    import cloudpickle
except ImportError:
    import pickle as cloudpickle

RETURN = 0
EXCEPTION = 1
TRACEBACK = 2
YIELD = 3
CANCEL = 4
ITER = 5
DONE = 6
QUIT = 7
CALL = 8


def is_in_process():
    return in_process_context.get()


def matches_suffixes(name, suffixes):
    for i in suffixes:
        if name.endswith(i):
            return True
    return False


def unkoil_process_gen(iterator, args, kwargs):
    input_queue = input_queue_context.get()
    out_queue = output_queue_context.get()
    send_to_queue(input_queue, ITER, (iterator, args, kwargs))

    while True:
        answer, args_kwargs_or_exception = get_from_queue(out_queue)
        if answer == YIELD:
            yield args_kwargs_or_exception
        elif answer == EXCEPTION:
            raise args_kwargs_or_exception
        elif answer == DONE:
            break
        elif answer == CANCEL:
            raise ProcessCancelledError("Cancelled during loop back of generator")
        else:
            raise KoilError(f"Unexpected answer: {answer}")


def unkoil_process_func(coro, args, kwargs):
    input_queue = input_queue_context.get()
    out_queue = output_queue_context.get()

    send_to_queue(input_queue, CALL, (coro, args, kwargs))
    while True:
        answer, func_args_kwargs_return_exception = get_from_queue(out_queue)
        if answer == RETURN:
            return func_args_kwargs_return_exception
        elif answer == EXCEPTION:
            raise func_args_kwargs_return_exception
        elif answer == CANCEL:
            raise ProcessCancelledError("Cancelled during loop back")
        else:
            raise KoilError(f"Unexpected answer: {answer}")


def send_to_queue(queue, task, func_args_kwargs_return_exception):
    cloudpickle_args = cloudpickle.dumps(func_args_kwargs_return_exception)
    queue.put((task, cloudpickle_args))


def get_from_queue(queue):
    task, cloudpickle_args = queue.get()
    if cloudpickle_args is None:
        func_args_kwargs_return_exception = None
    else:
        func_args_kwargs_return_exception = cloudpickle.loads(cloudpickle_args)
    return task, func_args_kwargs_return_exception


def gen_runner(queue, gen, *args, **kwargs):
    for i in gen(*args, **kwargs):
        send_to_queue(queue, YIELD, i)


def worker(input_queue, output_queue):
    output_queue_context.set(input_queue)
    input_queue_context.set(output_queue)
    in_process_context.set(True)
    while True:
        # Wait for a task
        task, func_args_kwargs_return_exception = get_from_queue(input_queue)
        if task == CALL:
            func, args, kwargs = func_args_kwargs_return_exception
            try:
                result = func(*args, **kwargs)
                send_to_queue(output_queue, RETURN, result)
            except Exception as e:
                send_to_queue(output_queue, EXCEPTION, e)

        if task == ITER:
            gen, args, kwargs = func_args_kwargs_return_exception
            try:
                gen_runner(output_queue, gen, *args, **kwargs)
                send_to_queue(output_queue, DONE, None)
            except Exception as e:
                send_to_queue(output_queue, EXCEPTION, e)

        if task == CANCEL:
            exception = func_args_kwargs_return_exception
            try:
                raise ProcessCancelledError("from worker") from exception
            except Exception as e:
                send_to_queue(output_queue, EXCEPTION, e)

            break

        if task == EXCEPTION:
            raise func_args_kwargs_return_exception

        if task == QUIT:
            break


class KoiledProcess:
    """A class that allows to call functions in a separate process."""

    def __init__(self, max_workers=1):
        """Create a new KoiledProcess.

        Args:
            max_workers (int, optional): The number of workers for the put to queue calls to use. Defaults to 1.
        """

        # Create the thread pool (we propably only need one thread?)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        # Create queues for communication
        self.input_queue = multiprocessing.Queue()
        self.output_queue = multiprocessing.Queue()

        # Start the worker process
        self.worker_process = multiprocessing.Process(
            target=worker, args=(self.input_queue, self.output_queue)
        )

        self.loop = asyncio.get_event_loop()
        self.loop.run_in_executor
        self.started = False

    async def __aenter__(self):
        self.started = True
        self.worker_process.start()
        return self

    async def __aexit__(self, *args, **kwargs):
        if self.started:
            self.quit()

    async def get_output(self):
        return await asyncio.get_event_loop().run_in_executor(
            self.executor, get_from_queue, self.output_queue
        )

    async def call(self, func, *args, **kwargs):
        assert self.started, "You need to start the KoilProcess first"
        # Send the function and arguments to the worker process
        try:
            send_to_queue(self.input_queue, CALL, (func, args, kwargs))

            # Wait for the result and then execute the callback
            while True:
                task, func_args_kwargs_return_exception = await self.get_output()

                if task == RETURN:
                    return func_args_kwargs_return_exception

                if task == EXCEPTION:
                    raise func_args_kwargs_return_exception

                if task == CALL:
                    func, args, kwargs = func_args_kwargs_return_exception
                    try:
                        result = await func(*args, **kwargs)
                        send_to_queue(self.input_queue, RETURN, result)
                    except Exception as e:
                        send_to_queue(self.input_queue, EXCEPTION, e)

                if task == ITER:
                    func, args, kwargs = func_args_kwargs_return_exception
                    try:
                        async for result in func(*args, **kwargs):
                            send_to_queue(self.input_queue, YIELD, result)

                        send_to_queue(self.input_queue, DONE, None)
                    except Exception as e:
                        send_to_queue(self.input_queue, EXCEPTION, e)

        except asyncio.CancelledError as e:
            send_to_queue(self.input_queue, CANCEL, e)
            task, args = await self.get_output()
            if task == EXCEPTION:
                if isinstance(args, ProcessCancelledError):
                    raise e
            else:
                raise args

    async def iter(self, func, *args, **kwargs):
        assert self.started, "You need to start the KoilProcess first"
        # Send the function and arguments to the worker process
        try:
            send_to_queue(self.input_queue, ITER, (func, args, kwargs))

            # Wait for the result and then execute the callback
            while True:
                task, func_args_kwargs_return_exception = await self.get_output()

                if task == DONE:
                    break

                if task == YIELD:
                    yield func_args_kwargs_return_exception

                if task == CALL:
                    func, args, kwargs = func_args_kwargs_return_exception
                    try:
                        result = await func(*args, **kwargs)
                        send_to_queue(self.input_queue, RETURN, result)
                    except Exception as e:
                        send_to_queue(self.input_queue, EXCEPTION, e)

                if task == EXCEPTION:
                    raise func_args_kwargs_return_exception

                if task == ITER:
                    func, args, kwargs = func_args_kwargs_return_exception
                    try:
                        async for result in func(*args, **kwargs):
                            send_to_queue(self.input_queue, YIELD, result)

                        send_to_queue(self.input_queue, DONE, None)
                    except Exception as e:
                        send_to_queue(self.input_queue, EXCEPTION, e)

        except asyncio.CancelledError as e:
            send_to_queue(self.input_queue, CANCEL, e)
            task, args = await self.get_output()
            if task == EXCEPTION:
                if isinstance(args, ProcessCancelledError):
                    raise e
            else:
                raise args

    def quit(self):
        # Send quit message to worker process
        send_to_queue(self.input_queue, QUIT, None)
        self.worker_process.join()
