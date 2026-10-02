"""SQLite 数据层：建表与连接管理。

关键约束直接落在数据库层，作为业务规则的最后一道防线：
- dose_execution 的部分唯一索引：同一服药格至多一条 TAKEN（有效执行）记录，
  即使两个线程并发也只能留下一条；
- boxes / orders / staff / residents 的业务唯一键；
- 全部表开启外键约束。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 员工：医师 / 药师 / 护理员
CREATE TABLE IF NOT EXISTS staff (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,          -- 工号
    name    TEXT NOT NULL,
    role    TEXT NOT NULL CHECK (role IN ('DOCTOR','PHARMACIST','NURSE'))
);

-- 住户
CREATE TABLE IF NOT EXISTS residents (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,          -- 住户编号
    name    TEXT NOT NULL,
    bed_no  TEXT NOT NULL UNIQUE,          -- 床位号（交接与固定格位依据）
    room    TEXT,
    status  TEXT NOT NULL DEFAULT 'IN' CHECK (status IN ('IN','OUT'))  -- 在家 / 外出
);

-- 住户用药禁忌（药师签发前必须核对）
CREATE TABLE IF NOT EXISTS contraindications (
    id          INTEGER PRIMARY KEY,
    resident_id INTEGER NOT NULL REFERENCES residents(id),
    med_name    TEXT NOT NULL,
    reason      TEXT NOT NULL
);

-- 医嘱（版本化）。同一住户至多一条 ACTIVE；交付新版本后旧版本变 SUPERSEDED。
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    resident_id INTEGER NOT NULL REFERENCES residents(id),
    version     INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','STOPPED')),
    note        TEXT,
    created_by  INTEGER NOT NULL REFERENCES staff(id),
    created_at  TEXT NOT NULL,
    UNIQUE (resident_id, version)
);

-- 医嘱条目；item 级状态支持"临时停用某一种药"
CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    seq         INTEGER NOT NULL,
    med_name    TEXT NOT NULL,            -- 药名（始终可见）
    spec        TEXT NOT NULL,            -- 规格，如 5mg / 0.5g
    dose_qty    REAL NOT NULL,            -- 每次数量
    unit        TEXT NOT NULL DEFAULT '片',
    meals       TEXT NOT NULL,            -- JSON 数组，服用餐次
    status      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','STOPPED'))
);

-- 药盒：住户 × 日期唯一，绑定医嘱版本快照
CREATE TABLE IF NOT EXISTS boxes (
    id               INTEGER PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    resident_id      INTEGER NOT NULL REFERENCES residents(id),
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    version_snapshot INTEGER NOT NULL,
    bed_no           TEXT NOT NULL,       -- 固定床位格位
    med_date         TEXT NOT NULL,       -- 用药日期 YYYY-MM-DD
    status           TEXT NOT NULL CHECK (
                       status IN ('DRAFT','PACKED','ISSUED','VOID')),
    preparer_id      INTEGER REFERENCES staff(id),  -- 配药人
    checker_id       INTEGER REFERENCES staff(id),  -- 复核 / 签发药师
    photo_path       TEXT,                -- 本地封盒照片
    prepared_at      TEXT,
    issued_at        TEXT,
    void_reason      TEXT,
    created_at       TEXT NOT NULL
);
-- 同一住户同一天至多有一个"有效"药盒；旧版作废盒保留供审计，允许重建。
CREATE UNIQUE INDEX IF NOT EXISTS idx_boxes_resident_date
    ON boxes(resident_id, med_date) WHERE status <> 'VOID';

-- 服药格：盒内固定格位 = 餐次 × 床位
CREATE TABLE IF NOT EXISTS box_cells (
    id            INTEGER PRIMARY KEY,
    box_id        INTEGER NOT NULL REFERENCES boxes(id),
    slot          TEXT NOT NULL,          -- 早餐 / 午餐 / 晚餐 / 睡前
    position      INTEGER NOT NULL,       -- 格位序号
    bed_no        TEXT NOT NULL,
    order_item_id INTEGER REFERENCES order_items(id),
    med_name      TEXT NOT NULL,          -- 药名（格位上始终可见）
    spec          TEXT NOT NULL,          -- 规格
    expected_qty  REAL NOT NULL,          -- 应摆数量
    actual_qty    REAL,                   -- 扫码实读数量
    scan_flag     TEXT,                   -- OK / WRONG_CELL / WRONG_MED /
                                          -- SPEC_MISMATCH / QTY_MISMATCH / CRUSHED
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
                    'PENDING','SCANNED_OK','BLOCKED',
                    'TAKEN','REFUSED','DEFERRED','MISSED','VOID')),
    UNIQUE (box_id, slot, order_item_id)
);

-- 扫码原始读数（模拟适配器写入）
CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY,
    cell_id       INTEGER NOT NULL REFERENCES box_cells(id),
    read_slot     TEXT NOT NULL,          -- 码中读出的格位
    read_med      TEXT NOT NULL,
    read_spec     TEXT NOT NULL,
    read_qty      REAL NOT NULL,
    flag          TEXT NOT NULL,
    scanner       TEXT NOT NULL,
    at            TEXT NOT NULL
);

-- 床位交接记录
CREATE TABLE IF NOT EXISTS handovers (
    id      INTEGER PRIMARY KEY,
    box_id  INTEGER NOT NULL REFERENCES boxes(id),
    from_id INTEGER REFERENCES staff(id),
    to_id   INTEGER NOT NULL REFERENCES staff(id),
    at      TEXT NOT NULL
);

-- 床旁执行记录（签收 / 拒服 / 暂缓）
CREATE TABLE IF NOT EXISTS dose_executions (
    id          INTEGER PRIMARY KEY,
    cell_id     INTEGER NOT NULL REFERENCES box_cells(id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('TAKEN','REFUSED','DEFERRED')),
    staff_id    INTEGER NOT NULL REFERENCES staff(id),
    witness_id  INTEGER REFERENCES staff(id),   -- 外出等异常双人复核
    reason      TEXT,
    created_at  TEXT NOT NULL
);
-- 硬约束：同一服药格至多一条"有效执行"（签收）记录，并发下也成立。
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_taken_per_cell
    ON dose_executions(cell_id) WHERE outcome = 'TAKEN';

-- 异常追踪（碎裂 / 错放 / 数量不符 / 拒服 / 暂缓 / 外出 / 漏签）
CREATE TABLE IF NOT EXISTS exceptions (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    ref_type    TEXT NOT NULL,            -- CELL / BOX / ORDER
    ref_id      INTEGER NOT NULL,
    resident_id INTEGER REFERENCES residents(id),
    status      TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED')),
    opened_by   INTEGER NOT NULL REFERENCES staff(id),
    reason      TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    resolved_by INTEGER REFERENCES staff(id),
    resolution  TEXT,
    closed_at   TEXT
);

-- 进程内提醒 / 漏签扫描留痕
CREATE TABLE IF NOT EXISTS reminders (
    id      INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,                -- DUE（临期提醒）/ MISSED（漏签）
    ref     TEXT NOT NULL,
    message TEXT NOT NULL,
    at      TEXT NOT NULL
);

-- 签发链：每个关键动作追加一行，prev_hash 串成哈希链
CREATE TABLE IF NOT EXISTS chain (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    event_type TEXT NOT NULL,
    ref        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
