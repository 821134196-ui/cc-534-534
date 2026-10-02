"""封盒照片：本地文件存储。

不依赖任何图像库——写入一个轻量 PNG（纯色 + 时间戳信息编码在 tEXt 块中），
仅用于演示"照片落本地、路径入签发链"。真实环境替换 save_photo 即可。
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime
from pathlib import Path


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _make_seal_png(label: str, size: int = 256) -> bytes:
    """生成一张医疗白底 + 青色封签图案的 PNG。"""
    width = height = size
    # 简单的纯色位图：白底 + 青色边框
    raw = bytearray()
    border = 14
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            edge = x < border or x >= width - border or y < border or y >= height - border
            if edge:
                raw.extend((0, 121, 140))  # #00798C 青色
            else:
                raw.extend((255, 255, 255))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    text = f"SEALED\x00{label}".encode("utf-8", "replace")
    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", ihdr),
        _png_chunk(b"tEXt", text),
        _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
        _png_chunk(b"IEND", b""),
    ])


def save_seal_photo(photo_dir: str | Path, box_id: int,
                    operator: str, at: datetime | None = None) -> str:
    """把封盒照片写到本地目录，返回相对/绝对路径字符串（入库用）。"""
    at = at or datetime.now()
    photo_dir = Path(photo_dir)
    photo_dir.mkdir(parents=True, exist_ok=True)
    name = f"box_{box_id:04d}_sealed_{at.strftime('%Y%m%d%H%M%S')}.png"
    path = photo_dir / name
    label = f"box={box_id};by={operator};at={at.isoformat(timespec='seconds')}"
    path.write_bytes(_make_seal_png(label))
    return str(path)
