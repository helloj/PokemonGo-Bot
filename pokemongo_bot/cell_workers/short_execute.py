# -*- coding: utf-8 -*-
import os, signal
from time import time
from random import uniform
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.worker_result import WorkerResult


DURATION_MIN = 60

class ShortExecute(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def initialize(self):
        duration_min = self.config.get("duration_min", DURATION_MIN)
        duration_max = self.config.get("duration_max", 600)
        if duration_min < DURATION_MIN:
             duration_min = DURATION_MIN
        if duration_max < duration_min:
             duration_max = duration_min
        duration_time = int(uniform(duration_min, duration_max))
        self.stop_time = time() + duration_time
        self.emit_event(
            'next_sleep',
            formatted="Short execute {time} seconds",
            data={
                'time': str(duration_time)
            }
        )

    def work(self):
        if time() > self.stop_time:
            self.emit_event(
                'bot_exit',
                formatted='Short execute completed and exiting bot.'
            )
            os.kill(os.getpid(), signal.SIGINT)
        return WorkerResult.SUCCESS
