"""SQLite 连接与建表脚本。

签发链（医嘱版本 -> 装盒 -> 双人复核签发 -> 封盒 -> 交接 -> 床旁执行 -> 异常追踪）
全部落在同一数据库文件中，外键开启，关键约束由数据库保证。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 人员：配药员 / 药师 / 护理员 / 医生
CREATE TABLE IF NOT EXISTS users (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('packer','pharmacist','nurse','physician'))
);

-- 住户（固定床位）
CREATE TABLE IF NOT EXISTS residents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    bed_no          TEXT NOT NULL UNIQUE,
    allergies       TEXT NOT NULL DEFAULT '',
    away            INTEGER NOT NULL DEFAULT 0,
    away_reason     TEXT,
    expected_return TEXT
);

-- 医嘱版本：任何一次医嘱变更（新开 / 临时停用）都产生新版本，旧版本即刻作 superseded
CREATE TABLE IF NOT EXISTS order_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER NOT NULL UNIQUE,
    status     TEXT NOT NULL CHECK (status IN ('current','superseded')),
    note       TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL
);

-- 医嘱条目（药名 / 规格 / 剂量 / 餐次）。stop_version_no 记录在哪个版本被停用
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id      TEXT NOT NULL REFERENCES residents(id),
    med_name         TEXT NOT NULL,
    spec             TEXT NOT NULL,
    dose_text        TEXT NOT NULL,
    meal_slot        TEXT NOT NULL CHECK (meal_slot IN ('breakfast','lunch','dinner')),
    quantity         REAL NOT NULL DEFAULT 1,
    version_no       INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active','stopped')),
    stop_reason      TEXT,
    stop_version_no  INTEGER,
    created_at       TEXT NOT NULL
);

-- 药盒：一次（住户 × 日期 × 餐次 × 医嘱版本）只能有一个未作废药盒
CREATE TABLE IF NOT EXISTS boxes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no    INTEGER NOT NULL,
    resident_id   TEXT NOT NULL REFERENCES residents(id),
    bed_no        TEXT NOT NULL,
    meal_slot     TEXT NOT NULL,
    serve_date    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN
                    ('packed','signed','sealed','handed_over','completed','void')),
    packed_by     TEXT NOT NULL,
    checked_by    TEXT,
    sealed_photo  TEXT,
    void_reason   TEXT,
    created_at    TEXT NOT NULL,
    signed_at     TEXT,
    sealed_at     TEXT,
    handed_at     TEXT
);
-- 同住户同日同餐次只允许一个未作废药盒（旧版药盒作废后才允许用新版重装）
CREATE UNIQUE INDEX IF NOT EXISTS ux_box_active
    ON boxes(resident_id, serve_date, meal_slot)
    WHERE status <> 'void';

-- 药盒格位：按床位/餐次固定编号，药名规格剂量冗余落表，界面始终可见
CREATE TABLE IF NOT EXISTS box_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id    INTEGER NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
    cell_no   INTEGER NOT NULL,
    order_id  INTEGER NOT NULL REFERENCES orders(id),
    med_name  TEXT NOT NULL,
    spec      TEXT NOT NULL,
    dose_text TEXT NOT NULL,
    quantity  REAL NOT NULL,
    state     TEXT NOT NULL DEFAULT 'ok'
                CHECK (state IN ('ok','broken','misplaced','replaced')),
    UNIQUE(box_id, cell_no)
);

-- 床位交接记录
CREATE TABLE IF NOT EXISTS handoffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id      INTEGER NOT NULL REFERENCES boxes(id),
    from_nurse  TEXT NOT NULL,
    to_nurse    TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

-- 床旁执行：每个格位全局只能有一条有效执行记录（数据库级唯一）
CREATE TABLE IF NOT EXISTS administrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL UNIQUE REFERENCES box_items(id),
    box_id      INTEGER NOT NULL REFERENCES boxes(id),
    nurse_id    TEXT NOT NULL,
    result      TEXT NOT NULL CHECK (result IN ('taken','refused','deferred')),
    reason      TEXT,
    until_time  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 执行流水：被阻断的重复确认、暂缓->签收 等所有尝试都留痕
CREATE TABLE IF NOT EXISTS admin_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER REFERENCES box_items(id),
    box_id      INTEGER REFERENCES boxes(id),
    nurse_id    TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

-- 异常追踪（停药 / 碎药 / 错格 / 外出 / 拒服 / 暂缓 / 漏签 / 重复给药 / 旧版药盒）
CREATE TABLE IF NOT EXISTS exceptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('block','high','warn')),
    box_id      INTEGER REFERENCES boxes(id),
    item_id     INTEGER REFERENCES box_items(id),
    admin_id    INTEGER REFERENCES administrations(id),
    resident_id TEXT REFERENCES residents(id),
    status      TEXT NOT NULL CHECK (status IN ('open','closed')),
    detail      TEXT NOT NULL,
    resolution  TEXT,
    created_by  TEXT,
    resolved_by TEXT,
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_exc_open ON exceptions(status, code);

-- 扫码流水（模拟适配器返回的读数）
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nurse_id    TEXT,
    raw_code    TEXT NOT NULL,
    parsed_med  TEXT,
    matched     INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

-- 进程内提醒任务产生的提醒
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    box_id      INTEGER REFERENCES boxes(id),
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    if db_path.parent and str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
