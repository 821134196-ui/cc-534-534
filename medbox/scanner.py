"""模拟扫码适配器。

真实环境中扫码枪读取药袋条码/格位条码；这里由适配器返回读数，
测试可注入异常读数（错放格位 / 药名不符 / 规格不符 / 数量不符 / 药片碎裂）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class ScanReading:
    slot: str          # 码中读出的格位（餐次）
    med_name: str
    spec: str
    qty: float
    crushed: bool = False


class MockScanner:
    """默认读数 = 期望读数（全部正确）。

    用法：
      scanner.faults[cell_id] = {"crushed": True}
      scanner.faults[cell_id] = {"slot": "晚餐"}      # 错放格位
      scanner.faults[cell_id] = {"qty": 2}            # 数量不符
    """

    name = "MOCK-SCANNER-01"

    def __init__(self) -> None:
        self.faults: dict[int, dict] = {}

    def read(self, cell: sqlite3.Row) -> ScanReading:
        f = self.faults.get(cell["id"], {})
        return ScanReading(
            slot=f.get("slot", cell["slot"]),
            med_name=f.get("med_name", cell["med_name"]),
            spec=f.get("spec", cell["spec"]),
            qty=float(f.get("qty", cell["expected_qty"])),
            crushed=bool(f.get("crushed", False)),
        )

    def inject(self, cell_id: int, **fault) -> None:
        self.faults[cell_id] = fault

    def clear(self, cell_id: int | None = None) -> None:
        if cell_id is None:
            self.faults.clear()
        else:
            self.faults.pop(cell_id, None)
