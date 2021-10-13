from typing import Type
from koil.task import KoilTask


class KoilState(object):

    def __init__(self, threaded=False, prefer_task=False, prefer_block=False) -> None:
        self.threaded = threaded
        self.prefer_task = prefer_task
        self.prefer_block = prefer_block
        super().__init__()


    def get_task_class(self) -> Type[KoilTask]:
        return KoilTask