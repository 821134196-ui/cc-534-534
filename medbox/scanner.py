"""扫码适配器（模拟）。

真实环境对接二维码/RFID 扫描枪；本系统通过适配器隔离，
默认实现按固定规则解析模拟码串，便于离线演示与测试。

码规则（模拟）：
  MED:<药名>;<规格>;<批号>        正常药品码，可解析
  UNKNOWN:<任意>                  无法识别
  空串 / None                     读码失败
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScanResult:
    med_name: Optional[str]
    spec: Optional[str]
    batch: Optional[str]
    raw: str
    ok: bool
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "med_name": self.med_name, "spec": self.spec, "batch": self.batch,
            "raw": self.raw, "ok": self.ok, "error": self.error,
        }


class ScannerAdapter:
    PATTERN = re.compile(r"^MED:(?P<med>[^;]+);(?P<spec>[^;]*);(?P<batch>[^;]*)$")

    def read(self, code: Optional[str]) -> Optional[dict]:
        """返回解析字典；无法识别返回 None（调用方据此阻断）。"""
        if not code or not code.strip():
            return None
        code = code.strip()
        if code.startswith("UNKNOWN:"):
            return None
        m = self.PATTERN.match(code)
        if not m:
            return None
        return {
            "med_name": m.group("med").strip(),
            "spec": m.group("spec").strip(),
            "batch": m.group("batch").strip(),
        }

    def encode(self, med_name: str, spec: str, batch: str = "B2026") -> str:
        """生成模拟条码，供播种与测试使用。"""
        return f"MED:{med_name};{spec};{batch}"
