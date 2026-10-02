"""进程内提醒任务：后台守护线程，定时扫描漏签并登记 DUE 提醒。

不依赖外部调度器；进程退出即停止。提醒记录写入 SQLite reminders 表，
同时触发回调（UI 可轮询 /api/reminders）。
"""

from __future__ import annotations

import threading
from datetime import datetime

from .service import SLOTS, SLOT_TIMES, MedService, now_iso


class ReminderScheduler(threading.Thread):
    def __init__(self, svc: MedService, interval_sec: int = 60,
                 on_event=None):
        super().__init__(daemon=True, name="medbox-reminder")
        self.svc = svc
        self.interval = interval_sec
        self.on_event = on_event
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 调度线程不能因单次错误死掉
                print(f"[reminder] tick error: {exc}")

    def tick(self) -> dict:
        """执行一轮：为到达服药时刻的未执行格登记 DUE；超时未签登记 MISSED。"""
        due = 0
        now = datetime.now()
        today = now.date().isoformat()
        for slot in SLOTS:
            hh, mm = map(int, SLOT_TIMES[slot].split(":"))
            if (now.hour, now.minute) >= (hh, mm):
                row = self.svc.conn.execute(
                    "SELECT COUNT(*) c FROM box_cells bc JOIN boxes b ON b.id=bc.box_id"
                    " WHERE b.med_date=? AND bc.slot=? AND b.status='ISSUED'"
                    " AND bc.status='SCANNED_OK'",
                    (today, slot),
                ).fetchone()
                if row["c"]:
                    dup = self.svc.conn.execute(
                        "SELECT 1 FROM reminders WHERE kind='DUE' AND ref=? LIMIT 1",
                        (f"slot:{today}:{slot}",),
                    ).fetchone()
                    if not dup:
                        self.svc.conn.execute(
                            "INSERT INTO reminders(kind,ref,message,at) VALUES (?,?,?,?)",
                            ("DUE", f"slot:{today}:{slot}",
                             f"{slot}服药时间到，{row['c']} 格待执行", now_iso()),
                        )
                    due += row["c"]
        missed = self.svc.scan_missed(now)
        self.svc.conn.commit()
        result = {"due": due, "missed": missed}
        if self.on_event and (due or missed):
            self.on_event(result)
        return result
