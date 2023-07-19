from .vars import output_queue_context, input_queue_context, in_process_context
import contextvars
import multiprocessing
from .errors import ProcessCancelledError
from concurrent.futures import ThreadPoolExecutor
import asyncio
import dill
import cloudpickle

dill.Pickler.dumps, dill.Pickler.loads = dill.dumps, dill.loads
multiprocessing.reduction.ForkingPickler = dill.Pickler
multiprocessing.reduction.dump = dill.dump


def send_to_queue(queue, task, *args):
    cloudpickle_args = cloudpickle.dumps(args)
    queue.put((task, cloudpickle_args))


def get_from_queue(queue):
    task, cloudpickle_args = queue.get()
    if cloudpickle_args is None:
        args = ()
    else:
        args = cloudpickle.loads(cloudpickle_args)
    return task, args


def worker(context, input_queue, output_queue):
    for ctx, value in context.items():
        ctx.set(value)

    output_queue_context.set(input_queue)
    input_queue_context.set(output_queue)
    in_process_context.set(True)
    while True:
        # Wait for a task
        z = get_from_queue(input_queue)
        task, args = z
        if task == "call":
            func, args, kwargs = args
            try:
                result = func(*args, **kwargs)
                send_to_queue(output_queue, "return", result)
            except Exception as e:
                send_to_queue(output_queue, "err", e)

        if task == "iter":
            func, args, kwargs = args
            try:
                for i in func(*args, **kwargs):
                    send_to_queue(output_queue, "yield", i)
                send_to_queue(output_queue, "done", None)
            except Exception as e:
                send_to_queue(output_queue, "err", e)

        if task == "cancel":
            z = ProcessCancelledError("from worker")
            send_to_queue(output_queue, "err", z)
            break

        if task == "quit":
            break


class KoiledProcess:
    def __init__(self):
        cpu_count = multiprocessing.cpu_count()
        self.executor = ThreadPoolExecutor(max_workers=cpu_count)
        # Create queues for communication
        self.input_queue = multiprocessing.Queue()
        self.output_queue = multiprocessing.Queue()

        # Start the worker process
        context = contextvars.copy_context()
        self.worker_process = multiprocessing.Process(
            target=worker, args=(context, self.input_queue, self.output_queue)
        )
        self.worker_process.start()

    async def get_output(self):
        print("GETTING OUTPUT")
        return await asyncio.get_event_loop().run_in_executor(
            self.executor, get_from_queue, self.output_queue
        )

    async def call(self, func, *args, **kwargs):
        # Send the function and arguments to the worker process
        try:
            send_to_queue(self.input_queue, "call", func, args, kwargs)

            # Wait for the result and then execute the callback
            while True:
                task, args = await self.get_output()
                print("TASK", task, args)

                if task == "return":
                    return args

                if task == "err":
                    raise args

                if task == "call":
                    func, args, kwargs = args

                    try:
                        result = await func(*args, **kwargs)
                        send_to_queue(self.input_queue, "return", result)
                    except Exception as e:
                        send_to_queue(self.input_queue, "err", e)

                if task == "iter":
                    func, args, kwargs = args
                    async for result in func(*args, **kwargs):
                        send_to_queue(self.input_queue, "yield", result)

                    send_to_queue(self.input_queue, "done", None)

        except asyncio.CancelledError as e:
            send_to_queue(self.input_queue, "cancel", None)
            task, args = await self.get_output()
            if task == "err":
                if isinstance(args, ProcessCancelledError):
                    raise e
            else:
                raise args

    async def iter(self, func, *args, **kwargs):
        # Send the function and arguments to the worker process
        try:
            send_to_queue(self.input_queue, "iter", func, args, kwargs)

            # Wait for the result and then execute the callback
            while True:
                task, args = await self.get_output()

                if task == "done":
                    break

                if task == "yield":
                    yield args

                if task == "call":
                    func, args, kwargs = args
                    result = await func(*args, **kwargs)
                    send_to_queue(self.input_queue, "return", result)

                if task == "err":
                    raise args

                if task == "iter":
                    func, args, kwargs = args
                    async for result in func(*args, **kwargs):
                        send_to_queue(self.input_queue, "yield", result)

                    send_to_queue(self.input_queue, "done", None)

        except asyncio.CancelledError as e:
            send_to_queue(self.input_queue, "cancel", None)
            task, args = await self.get_output()
            if task == "err":
                if isinstance(args, ProcessCancelledError):
                    raise e
            else:
                raise args

    def quit(self):
        # Send quit message to worker process
        self.input_queue.put_nowait(("quit", None))
        self.worker_process.join()
