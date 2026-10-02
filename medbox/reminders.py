"""进程内提醒任务：后台线程周期性触发暂缓到期 / 漏签检查。

不引入任何外部调度器（无 cron / celery）；daemon 线程随进程退出。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional


class ReminderScheduler:
    def __init__(self, tick: Callable[[], dict], interval_seconds: float = 30.0):
        self.tick = tick
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[dict] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="medbox-reminder", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.last_result = self.tick()
            except Exception:  # 提醒失败不能拖垮主进程
                pass

    def run_once(self) -> dict:
        self.last_result = self.tick()
        return self.last_result

    def stop(self) -> None:
        self._stop.set()
