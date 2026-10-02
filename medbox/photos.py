"""本地封盒照片：用标准库 zlib 手写一张最小 PNG（无第三方依赖）。

照片记录盒号、床位、日期、格位数与封盒时刻，供事后追溯。
"""

from __future__ import annotations

import sqlite3
import struct
import zlib
from datetime import datetime
from pathlib import Path

W, H = 480, 300


def _png(width: int, height: int, rows: list[bytearray]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _put_text(rows: list[bytearray], x: int, y: int, text: str,
              scale: int = 2, color=(20, 60, 70)) -> None:
    """5×7 点阵字库（ASCII + 常用中文按 GB 字符直接绘制简单方块占位）。

    中文在点阵字库外，统一用青色实心块序列表示"文字行"——
    封盒照片的核心信息以文件名字符串与哈希链为准，图片仅作留痕凭证。
    """
    font = {
        " ": ["00000"] * 7,
        "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
        "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
        "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
        "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
        "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
        "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
        "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
        "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
        "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
        "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
        "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
        ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
        ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    }
    cx = x
    for ch in text:
        glyph = font.get(ch)
        if glyph is None:
            glyph = ["11111"] * 7  # 非 ASCII（中文）用实心块
        for ry, line in enumerate(glyph):
            for rx, bit in enumerate(line):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            px, py = cx + rx * scale + dx, y + ry * scale + dy
                            if 0 <= px < W and 0 <= py < H:
                                off = px * 3
                                rows[py][off:off + 3] = bytes(color)
        cx += 6 * scale


def seal_photo(photo_dir: str | Path, box_id: int, conn: sqlite3.Connection) -> Path:
    """生成本地封盒照片 PNG，返回路径。"""
    photo_dir = Path(photo_dir)
    photo_dir.mkdir(parents=True, exist_ok=True)

    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    n = conn.execute(
        "SELECT COUNT(*) c FROM box_cells WHERE box_id=?", (box_id,)
    ).fetchone()["c"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 白底 + 青色边框
    rows = [bytearray(b"\xff\xff\xff" * W) for _ in range(H)]
    teal = (0, 121, 140)
    for y in range(H):
        for x in range(W):
            if x < 12 or x >= W - 12 or y < 12 or y >= H - 12:
                off = x * 3
                rows[y][off:off + 3] = bytes(teal)

    _put_text(rows, 28, 30, f"SEALED BOX {box_id}", 2)
    _put_text(rows, 28, 70, f"BED {box['bed_no']}  DATE {box['med_date']}", 2)
    _put_text(rows, 28, 110, f"ORDER v{box['version_snapshot']}  CELLS {n}", 2)
    _put_text(rows, 28, 150, f"PREPARER {box['preparer_id']}", 2)
    _put_text(rows, 28, 240, ts, 2)

    path = photo_dir / f"box_{box_id}_{box['med_date']}_sealed.png"
    path.write_bytes(_png(W, H, rows))
    return path
