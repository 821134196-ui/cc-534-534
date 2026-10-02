"""核心业务规则：医嘱版本 → 装盒 → 双人复核签发 → 床旁执行 → 异常闭环。

所有会改变状态的动作都写入 chain 哈希链（SQLite 保存签发链）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .photos import seal_photo
from .scanner import MockScanner, ScanReading

# 固定餐次（药盒界面按餐次固定格位）
SLOTS = ["早餐", "午餐", "晚餐", "睡前"]
# 各餐次计划服药时刻（本地钟点表示）
SLOT_TIMES = {"早餐": "08:00", "午餐": "12:00", "晚餐": "18:00", "睡前": "21:00"}
# 超过计划时刻多少分钟未签收算"漏签"
MISSED_AFTER_MIN = 30

ROLE_DOCTOR, ROLE_PHARM, ROLE_NURSE = "DOCTOR", "PHARMACIST", "NURSE"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RuleViolation(Exception):
    """业务规则被违反（阻断）。"""


class MedService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        scanner: MockScanner | None = None,
        photo_dir: str | Path = "data/photos",
    ):
        self.conn = conn
        self.scanner = scanner or MockScanner()
        self.photo_dir = Path(photo_dir)

    # ------------------------------------------------------------------ #
    # 基础资料
    # ------------------------------------------------------------------ #
    def add_staff(self, code: str, name: str, role: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO staff(code,name,role) VALUES (?,?,?)", (code, name, role)
        )
        self.conn.commit()
        return cur.lastrowid

    def add_resident(self, code: str, name: str, bed_no: str, room: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO residents(code,name,bed_no,room,status) VALUES (?,?,?,?,'IN')",
            (code, name, bed_no, room),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_resident_out(self, resident_id: int, out: bool) -> None:
        """住户外出 / 回院。外出期间服药格执行将被阻断。"""
        status = "OUT" if out else "IN"
        self.conn.execute(
            "UPDATE residents SET status=? WHERE id=?", (status, resident_id)
        )
        self.conn.commit()

    def add_contraindication(self, resident_id: int, med_name: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO contraindications(resident_id,med_name,reason) VALUES (?,?,?)",
            (resident_id, med_name, reason),
        )
        self.conn.commit()

    def _staff(self, staff_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
        if row is None:
            raise RuleViolation(f"员工 {staff_id} 不存在")
        return row

    def _system_id(self) -> int:
        """系统/定时任务的操作人（扫码适配器、漏签扫描）。已在事务内时勿提交。"""
        row = self.conn.execute(
            "SELECT id FROM staff WHERE code='SYSTEM'"
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO staff(code,name,role) VALUES ('SYSTEM','系统任务','NURSE')"
        )
        return cur.lastrowid

    def _require_role(self, staff_id: int, role: str) -> sqlite3.Row:
        row = self._staff(staff_id)
        if row["role"] != role:
            raise RuleViolation(f"{row['name']} 的角色是 {row['role']}，需要 {role}")
        return row

    # ------------------------------------------------------------------ #
    # 签发链
    # ------------------------------------------------------------------ #
    def append_chain(self, event_type: str, ref: str, payload: dict) -> None:
        prev = self.conn.execute(
            "SELECT hash FROM chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["hash"] if prev else "GENESIS"
        body = json.dumps(
            {"at": now_iso(), "event_type": event_type, "ref": ref,
             "payload": payload, "prev_hash": prev_hash},
            ensure_ascii=False, sort_keys=True,
        )
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO chain(at,event_type,ref,payload,prev_hash,hash)"
            " VALUES (?,?,?,?,?,?)",
            (now_iso(), event_type, ref, json.dumps(payload, ensure_ascii=False),
             prev_hash, h),
        )

    @staticmethod
    def verify_chain(conn: sqlite3.Connection) -> list[int]:
        """重算哈希链；返回被篡改行的 seq 列表（空列表=完整）。"""
        bad: list[int] = []
        prev_hash = "GENESIS"
        for row in conn.execute("SELECT * FROM chain ORDER BY seq"):
            body = json.dumps(
                {"at": row["at"], "event_type": row["event_type"], "ref": row["ref"],
                 "payload": json.loads(row["payload"]),
                 "prev_hash": prev_hash},
                ensure_ascii=False, sort_keys=True,
            )
            if hashlib.sha256(body.encode()).hexdigest() != row["hash"]:
                bad.append(row["seq"])
            prev_hash = row["hash"]
        return bad

    # ------------------------------------------------------------------ #
    # 医嘱版本（仅医师可写；护理员无权改医嘱）
    # ------------------------------------------------------------------ #
    def create_order(
        self,
        resident_id: int,
        doctor_id: int,
        items: list[dict],
        note: str = "",
    ) -> int:
        """交付新版医嘱：旧版变 SUPERSEDED，旧版未执行完的药盒立即作废。"""
        self._require_role(doctor_id, ROLE_DOCTOR)
        with self.conn:  # 原子事务
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM orders WHERE resident_id=?",
                (resident_id,),
            ).fetchone()
            version = row["v"]
            cur = self.conn.execute(
                "INSERT INTO orders(resident_id,version,status,note,created_by,created_at)"
                " VALUES (?,?,'ACTIVE',?,?,?)",
                (resident_id, version, note, doctor_id, now_iso()),
            )
            order_id = cur.lastrowid
            for seq, it in enumerate(items, 1):
                self.conn.execute(
                    "INSERT INTO order_items(order_id,seq,med_name,spec,dose_qty,unit,meals)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (order_id, seq, it["med_name"], it["spec"],
                     float(it["dose_qty"]), it.get("unit", "片"),
                     json.dumps(it["meals"], ensure_ascii=False)),
                )
            self.conn.execute(
                "UPDATE orders SET status='SUPERSEDED' WHERE resident_id=? AND id<>?",
                (resident_id, order_id),
            )
            # 旧医嘱版本的药盒不能继续使用
            old_boxes = self.conn.execute(
                "SELECT id FROM boxes WHERE resident_id=? AND order_id<>?"
                " AND status IN ('DRAFT','PACKED','ISSUED')",
                (resident_id, order_id),
            ).fetchall()
            for b in old_boxes:
                self._void_box_locked(b["id"], f"医嘱升级到 v{version}，旧版药盒作废")
            self.append_chain(
                "ORDER_DELIVERED", f"order:{order_id}",
                {"resident_id": resident_id, "version": version,
                 "items": items, "by": doctor_id},
            )
        return order_id

    def suspend_item(self, order_item_id: int, doctor_id: int) -> None:
        """医嘱临时停用某个条目（仅医师）。含该药的待发/在用药盒立即阻断。"""
        self._require_role(doctor_id, ROLE_DOCTOR)
        with self.conn:
            item = self.conn.execute(
                "SELECT oi.*, o.resident_id FROM order_items oi"
                " JOIN orders o ON o.id=oi.order_id WHERE oi.id=?",
                (order_item_id,),
            ).fetchone()
            if item is None:
                raise RuleViolation("医嘱条目不存在")
            self.conn.execute(
                "UPDATE order_items SET status='STOPPED' WHERE id=?", (order_item_id,)
            )
            cells = self.conn.execute(
                "SELECT bc.id, bc.box_id FROM box_cells bc JOIN boxes b ON b.id=bc.box_id"
                " WHERE bc.order_item_id=? AND b.status IN ('DRAFT','PACKED','ISSUED')"
                " AND bc.status NOT IN ('TAKEN','VOID')",
                (order_item_id,),
            ).fetchall()
            for c in cells:
                self.conn.execute(
                    "UPDATE box_cells SET status='BLOCKED' WHERE id=?", (c["id"],)
                )
                self._open_exc_locked(
                    "ORDER_SUSPENDED", "CELL", c["id"], item["resident_id"],
                    doctor_id, f"医嘱临时停用：{item['med_name']}，该格阻断待复核",
                )
            self.append_chain(
                "ORDER_ITEM_SUSPENDED", f"order_item:{order_item_id}",
                {"by": doctor_id, "med_name": item["med_name"]},
            )

    # ------------------------------------------------------------------ #
    # 建盒 / 备药 / 扫码
    # ------------------------------------------------------------------ #
    def create_box(self, resident_id: int, med_date: str) -> int:
        """按当前 ACTIVE 医嘱版本为某住户某日建盒，生成餐次固定格位。"""
        order = self.conn.execute(
            "SELECT * FROM orders WHERE resident_id=? AND status='ACTIVE'"
            " ORDER BY version DESC LIMIT 1",
            (resident_id,),
        ).fetchone()
        if order is None:
            raise RuleViolation("该住户没有 ACTIVE 医嘱，无法建盒")
        resident = self.conn.execute(
            "SELECT * FROM residents WHERE id=?", (resident_id,)
        ).fetchone()
        dup = self.conn.execute(
            "SELECT id FROM boxes WHERE resident_id=? AND med_date=? AND status<>'VOID'",
            (resident_id, med_date),
        ).fetchone()
        if dup is not None:
            raise RuleViolation(f"{med_date} 的药盒已存在（box {dup['id']}）")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO boxes(code,resident_id,order_id,version_snapshot,bed_no,"
                "med_date,status,created_at) VALUES (?,?,?,?,?,?,'DRAFT',?)",
                (f"BOX-{resident['bed_no']}-{med_date}-v{order['version']}",
                 resident_id, order["id"],
                 order["version"], resident["bed_no"], med_date, now_iso()),
            )
            box_id = cur.lastrowid
            position = 0
            for slot in SLOTS:
                items = self.conn.execute(
                    "SELECT * FROM order_items WHERE order_id=? AND status='ACTIVE'"
                    " AND meals LIKE ? ORDER BY seq",
                    (order["id"], f'%"{slot}"%'),
                ).fetchall()
                for it in items:
                    position += 1
                    self.conn.execute(
                        "INSERT INTO box_cells(box_id,slot,position,bed_no,order_item_id,"
                        "med_name,spec,expected_qty,status)"
                        " VALUES (?,?,?,?,?,?,?,?,'PENDING')",
                        (box_id, slot, position, resident["bed_no"], it["id"],
                         it["med_name"], it["spec"], it["dose_qty"]),
                    )
            self.append_chain(
                "BOX_CREATED", f"box:{box_id}",
                {"resident_id": resident_id, "med_date": med_date,
                 "order_version": order["version"], "cells": position},
            )
        return box_id

    def scan_cell(self, cell_id: int) -> sqlite3.Row:
        """模拟扫码适配器读数并判定；异常读数阻断该格并开异常单。"""
        cell = self.conn.execute("SELECT * FROM box_cells WHERE id=?", (cell_id,)).fetchone()
        if cell is None:
            raise RuleViolation("服药格不存在")
        reading = self.scanner.read(cell)
        flag = self._classify(cell, reading)
        with self.conn:
            self.conn.execute(
                "INSERT INTO scans(cell_id,read_slot,read_med,read_spec,read_qty,"
                "flag,scanner,at) VALUES (?,?,?,?,?,?,?,?)",
                (cell_id, reading.slot, reading.med_name, reading.spec, reading.qty,
                 flag, self.scanner.name, now_iso()),
            )
            self.conn.execute(
                "UPDATE box_cells SET actual_qty=?, scan_flag=?,"
                " status=? WHERE id=?",
                (reading.qty, flag,
                 "SCANNED_OK" if flag == "OK" else "BLOCKED", cell_id),
            )
            if flag != "OK":
                box = self.conn.execute(
                    "SELECT resident_id FROM boxes WHERE id=?", (cell["box_id"],)
                ).fetchone()
                self._open_exc_locked(
                    flag, "CELL", cell_id, box["resident_id"], self._system_id(),
                    self._flag_message(flag, cell, reading),
                )
                self.append_chain(
                    "SCAN_BLOCKED", f"cell:{cell_id}",
                    {"flag": flag, "reading": reading.__dict__},
                )
        return self.conn.execute("SELECT * FROM box_cells WHERE id=?", (cell_id,)).fetchone()

    @staticmethod
    def _classify(cell: sqlite3.Row, r: ScanReading) -> str:
        if r.crushed:
            return "CRUSHED"
        if r.slot != cell["slot"]:
            return "WRONG_CELL"
        if r.med_name != cell["med_name"]:
            return "WRONG_MED"
        if r.spec != cell["spec"]:
            return "SPEC_MISMATCH"
        if abs(float(r.qty) - float(cell["expected_qty"])) > 1e-9:
            return "QTY_MISMATCH"
        return "OK"

    @staticmethod
    def _flag_message(flag: str, cell: sqlite3.Row, r: ScanReading) -> str:
        return {
            "CRUSHED": f"{cell['slot']}格 {cell['med_name']} 扫码发现药片碎裂，禁止发出",
            "WRONG_CELL": f"错放格位：{cell['slot']}格扫到属于「{r.slot}」的药",
            "WRONG_MED": f"药名不符：{cell['slot']}格应为 {cell['med_name']}，实读 {r.med_name}",
            "SPEC_MISMATCH": f"规格不符：{cell['med_name']} 应为 {cell['spec']}，实读 {r.spec}",
            "QTY_MISMATCH": f"数量不符：{cell['slot']}格 {cell['med_name']}"
                            f" 应为 {cell['expected_qty']}，实读 {r.qty}",
        }[flag]

    def repack_cell(self, cell_id: int, staff_id: int) -> sqlite3.Row:
        """异常格复核后重新摆药：清阻断、回 PENDING 等待重新扫码。

        已停用药品的格位不能重摆，须用 remove_cell 撤药。
        """
        self._staff(staff_id)
        item = self.conn.execute(
            "SELECT oi.status FROM box_cells bc LEFT JOIN order_items oi"
            " ON oi.id=bc.order_item_id WHERE bc.id=?",
            (cell_id,),
        ).fetchone()
        if item and item["status"] == "STOPPED":
            raise RuleViolation("该药已被医嘱停用，不能重摆，请执行撤药")
        with self.conn:
            self.conn.execute(
                "UPDATE box_cells SET status='PENDING', scan_flag=NULL, actual_qty=NULL"
                " WHERE id=?", (cell_id,),
            )
            self.append_chain(
                "CELL_REPACKED", f"cell:{cell_id}", {"by": staff_id}
            )
        return self.conn.execute("SELECT * FROM box_cells WHERE id=?", (cell_id,)).fetchone()

    def remove_cell(self, cell_id: int, pharmacist_id: int) -> None:
        """药师复核停用医嘱后撤药：格位置 VOID。须先闭环对应异常。"""
        self._require_role(pharmacist_id, ROLE_PHARM)
        with self.conn:
            cell = self.conn.execute(
                "SELECT * FROM box_cells WHERE id=?", (cell_id,)
            ).fetchone()
            if cell is None:
                raise RuleViolation("服药格不存在")
            if cell["status"] not in ("BLOCKED", "PENDING"):
                raise RuleViolation(f"格位状态 {cell['status']}，不能撤药")
            open_exc = self.conn.execute(
                "SELECT COUNT(*) n FROM exceptions WHERE ref_type='CELL'"
                " AND ref_id=? AND status='OPEN'", (cell_id,),
            ).fetchone()["n"]
            if open_exc:
                raise RuleViolation("撤药前须先闭环该格的异常单")
            self.conn.execute(
                "UPDATE box_cells SET status='VOID' WHERE id=?", (cell_id,)
            )
            self.append_chain(
                "CELL_REMOVED", f"cell:{cell_id}",
                {"by": pharmacist_id, "med_name": cell["med_name"]},
            )

    # ------------------------------------------------------------------ #
    # 封盒 / 双人复核签发
    # ------------------------------------------------------------------ #
    def pack(self, box_id: int, preparer_id: int) -> str:
        """配药人摆药完成并封盒拍照。所有格必须扫码通过且无未决异常。"""
        self._require_role(preparer_id, ROLE_PHARM)
        self._box_must_be_current(box_id)
        cells = self.conn.execute(
            "SELECT * FROM box_cells WHERE box_id=?", (box_id,)
        ).fetchall()
        pending = [c for c in cells if c["status"] in ("PENDING", "BLOCKED")]
        if pending:
            raise RuleViolation(
                "封盒被阻断：仍有格位未扫码通过 — "
                + "、".join(f"{c['slot']}-{c['med_name']}[{c['status']}]" for c in pending)
            )
        open_exc = self.conn.execute(
            "SELECT COUNT(*) n FROM exceptions WHERE ref_type='CELL'"
            " AND ref_id IN (SELECT id FROM box_cells WHERE box_id=?) AND status='OPEN'",
            (box_id,),
        ).fetchone()["n"]
        if open_exc:
            raise RuleViolation(f"封盒被阻断：本盒还有 {open_exc} 条未闭环异常")
        path = seal_photo(self.photo_dir, box_id, self.conn)
        with self.conn:
            self.conn.execute(
                "UPDATE boxes SET status='PACKED', preparer_id=?, prepared_at=?,"
                " photo_path=? WHERE id=?",
                (preparer_id, now_iso(), str(path), box_id),
            )
            self.append_chain(
                "BOX_PACKED", f"box:{box_id}",
                {"by": preparer_id, "photo": str(path), "cells": len(cells)},
            )
        return str(path)

    def issue(self, box_id: int, checker_id: int) -> None:
        """药师核对药名/规格/数量/禁忌后签发；配药人与复核人必须不同。"""
        checker = self._require_role(checker_id, ROLE_PHARM)
        box = self.conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
        if box is None:
            raise RuleViolation("药盒不存在")
        self._box_must_be_current(box_id)  # 旧版作废盒优先报失效
        if box["status"] != "PACKED":
            raise RuleViolation(f"药盒状态为 {box['status']}，须先由配药人封盒（PACKED）")
        if box["preparer_id"] is None:
            raise RuleViolation("缺少配药人签署，不能签发")
        if box["preparer_id"] == checker_id:
            raise RuleViolation(
                f"双签被拒绝：配药人与复核人不能同为 {checker['name']}"
            )
        self._box_must_be_current(box_id)
        # 药师核对：药名 / 规格 / 数量（VOID 撤药格与已签收格除外）
        bad = self.conn.execute(
            "SELECT * FROM box_cells WHERE box_id=?"
            " AND status NOT IN ('SCANNED_OK','VOID','TAKEN')",
            (box_id,),
        ).fetchall()
        if bad:
            raise RuleViolation("核对不通过：存在未通过扫码的格位")
        # 禁忌核对
        resident_id = box["resident_id"]
        med_names = [r["med_name"] for r in self.conn.execute(
            "SELECT DISTINCT med_name FROM box_cells WHERE box_id=?", (box_id,))]
        contra = self.conn.execute(
            "SELECT * FROM contraindications WHERE resident_id=? AND med_name IN ({})"
            .format(",".join("?" * len(med_names))),
            (resident_id, *med_names),
        ).fetchall() if med_names else []
        if contra:
            raise RuleViolation(
                "禁忌核对不通过：" + "；".join(
                    f"{c['med_name']}（{c['reason']}）" for c in contra)
            )
        with self.conn:
            self.conn.execute(
                "UPDATE boxes SET status='ISSUED', checker_id=?, issued_at=? WHERE id=?",
                (checker_id, now_iso(), box_id),
            )
            self.append_chain(
                "BOX_ISSUED", f"box:{box_id}",
                {"preparer": box["preparer_id"], "checker": checker_id,
                 "photo": box["photo_path"]},
            )

    def _box_must_be_current(self, box_id: int) -> sqlite3.Row:
        box = self.conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
        if box is None:
            raise RuleViolation("药盒不存在")
        if box["status"] == "VOID":
            raise RuleViolation(f"药盒已作废（{box['void_reason']}），禁止操作")
        order = self.conn.execute(
            "SELECT status, version FROM orders WHERE id=?", (box["order_id"],)
        ).fetchone()
        if order["status"] != "ACTIVE":
            raise RuleViolation(
                f"盒中药嘱版本 v{order['version']} 已失效，旧版药盒不能继续使用"
            )
        return box

    def _void_box_locked(self, box_id: int, reason: str) -> None:
        """调用方须已持有事务。"""
        self.conn.execute(
            "UPDATE boxes SET status='VOID', void_reason=? WHERE id=?"
            " AND status<>'VOID'",
            (reason, box_id),
        )
        self.conn.execute(
            "UPDATE box_cells SET status='VOID' WHERE box_id=? AND status<>'TAKEN'",
            (box_id,),
        )
        self.append_chain("BOX_VOIDED", f"box:{box_id}", {"reason": reason})

    # ------------------------------------------------------------------ #
    # 床位交接
    # ------------------------------------------------------------------ #
    def handover(self, box_id: int, from_id: int | None, to_id: int) -> None:
        """护理员按床位交接药盒。"""
        self._staff(to_id)
        box = self._box_must_be_current(box_id)
        if box["status"] != "ISSUED":
            raise RuleViolation("仅已签发药盒可交接")
        with self.conn:
            self.conn.execute(
                "INSERT INTO handovers(box_id,from_id,to_id,at) VALUES (?,?,?,?)",
                (box_id, from_id, to_id, now_iso()),
            )
            self.append_chain(
                "HANDOVER", f"box:{box_id}", {"from": from_id, "to": to_id},
            )

    # ------------------------------------------------------------------ #
    # 床旁执行
    # ------------------------------------------------------------------ #
    def execute(
        self,
        cell_id: int,
        outcome: str,
        staff_id: int,
        reason: str = "",
        witness_id: int | None = None,
    ) -> dict:
        """床旁记录：TAKEN 签收 / REFUSED 拒服 / DEFERRED 暂缓。

        - 仅护理员执行；盒须已签发且医嘱版本仍有效；
        - 住户外出阻断，需双人（witness）复核方可暂缓；
        - 拒服、暂缓必须填原因，并开异常单追踪；
        - 同一格 TAKEN 由数据库唯一索引保证全局仅一条（含并发）。
        """
        nurse = self._require_role(staff_id, ROLE_NURSE)
        if outcome not in ("TAKEN", "REFUSED", "DEFERRED"):
            raise RuleViolation("执行结果只能是 TAKEN / REFUSED / DEFERRED")
        cell = self.conn.execute(
            "SELECT bc.*, b.status box_status, b.resident_id, b.order_id, b.bed_no"
            " FROM box_cells bc JOIN boxes b ON b.id=bc.box_id WHERE bc.id=?",
            (cell_id,),
        ).fetchone()
        if cell is None:
            raise RuleViolation("服药格不存在")
        self._box_must_be_current(cell["box_id"])
        if cell["box_status"] != "ISSUED":
            raise RuleViolation("药盒未签发，不能执行")
        if cell["status"] == "BLOCKED":
            raise RuleViolation("该格处于阻断状态，须先复核处理异常")

        resident = self.conn.execute(
            "SELECT * FROM residents WHERE id=?", (cell["resident_id"],)
        ).fetchone()
        if resident["status"] == "OUT" and outcome == "TAKEN":
            raise RuleViolation("住户外出中，不能签收；须双人复核后登记暂缓")
        if outcome == "DEFERRED" and resident["status"] == "OUT" and witness_id is None:
            raise RuleViolation("外出暂缓需第二人同时确认（witness）")
        if witness_id is not None and witness_id == staff_id:
            raise RuleViolation("双人确认不能为同一人")
        if outcome in ("REFUSED", "DEFERRED") and not reason.strip():
            raise RuleViolation("拒服 / 暂缓必须填写原因")

        with self.conn:
            if witness_id is not None:
                self._staff(witness_id)
            try:
                cur = self.conn.execute(
                    "INSERT INTO dose_executions(cell_id,outcome,staff_id,witness_id,"
                    "reason,created_at) VALUES (?,?,?,?,?,?)",
                    (cell_id, outcome, staff_id, witness_id, reason, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise RuleViolation(
                    "重复给药阻断：该服药格已存在有效签收记录"
                ) from None
            exec_id = cur.lastrowid
            if outcome == "TAKEN":
                self.conn.execute(
                    "UPDATE box_cells SET status='TAKEN' WHERE id=?", (cell_id,)
                )
                # 签收即自动闭环该格未决的拒服/暂缓异常
                self.conn.execute(
                    "UPDATE exceptions SET status='RESOLVED', resolved_by=?,"
                    " resolution='已补服签收闭环', closed_at=?"
                    " WHERE ref_type='CELL' AND ref_id=? AND status='OPEN'",
                    (staff_id, now_iso(), cell_id),
                )
            else:
                self.conn.execute(
                    "UPDATE box_cells SET status=? WHERE id=?", (outcome, cell_id)
                )
                self._open_exc_locked(
                    outcome, "CELL", cell_id, cell["resident_id"], staff_id,
                    f"{nurse['name']} 登记：{reason}"
                    + ("（双人复核" + ("，住户外出" if resident["status"] == "OUT" else "")
                       + "）" if witness_id else ""),
                )
            self.append_chain(
                "DOSE_EXECUTED", f"cell:{cell_id}",
                {"exec_id": exec_id, "outcome": outcome, "by": staff_id,
                 "witness": witness_id, "reason": reason},
            )
        return {"exec_id": exec_id, "status": outcome}

    # ------------------------------------------------------------------ #
    # 异常追踪
    # ------------------------------------------------------------------ #
    def _open_exc_locked(self, etype, ref_type, ref_id, resident_id, opened_by, reason):
        code = f"EXC-{ref_type}-{ref_id}-{etype}"
        exists = self.conn.execute(
            "SELECT id FROM exceptions WHERE code=? AND status='OPEN'", (code,)
        ).fetchone()
        if exists:
            return
        self.conn.execute(
            "INSERT INTO exceptions(code,type,ref_type,ref_id,resident_id,status,"
            "opened_by,reason,opened_at) VALUES (?,?,?,?,?,'OPEN',?,?,?)",
            (code, etype, ref_type, ref_id, resident_id, opened_by, reason, now_iso()),
        )

    def resolve_exception(self, exc_id: int, staff_id: int, resolution: str) -> None:
        """异常复核闭环：填写处理结果。拒服闭环的必经一步。"""
        self._staff(staff_id)
        if not resolution.strip():
            raise RuleViolation("闭环必须填写处理结果")
        with self.conn:
            exc = self.conn.execute(
                "SELECT * FROM exceptions WHERE id=?", (exc_id,)
            ).fetchone()
            if exc is None:
                raise RuleViolation("异常单不存在")
            if exc["status"] == "RESOLVED":
                raise RuleViolation("该异常已闭环")
            # 扫码类异常闭环后允许重新摆药扫码；格位保持 BLOCKED 直到 repack
            self.conn.execute(
                "UPDATE exceptions SET status='RESOLVED', resolved_by=?,"
                " resolution=?, closed_at=? WHERE id=?",
                (staff_id, resolution, now_iso(), exc_id),
            )
            self.append_chain(
                "EXCEPTION_RESOLVED", f"exception:{exc_id}",
                {"by": staff_id, "resolution": resolution, "type": exc["type"]},
            )

    def open_exceptions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT e.*, r.name resident_name, r.bed_no FROM exceptions e"
            " LEFT JOIN residents r ON r.id=e.resident_id"
            " WHERE e.status='OPEN' ORDER BY e.id"
        ))

    # ------------------------------------------------------------------ #
    # 漏签扫描（进程内提醒任务调用）
    # ------------------------------------------------------------------ #
    def scan_missed(self, at: datetime | None = None) -> int:
        """超过餐次计划窗口仍未签收的格位：标记 MISSED 并开异常，阻断后续放行。"""
        at = at or datetime.now()
        count = 0
        # 候选（日期, 餐次）：当天已过窗口的餐次；凌晨（08:00 前）补查昨日睡前。
        candidates: list[tuple[str, str]] = []
        for slot in SLOTS:
            hh, mm = map(int, SLOT_TIMES[slot].split(":"))
            deadline = at.replace(hour=hh, minute=mm) + timedelta(minutes=MISSED_AFTER_MIN)
            if at >= deadline:
                candidates.append((at.date().isoformat(), slot))
        if at.hour < int(SLOT_TIMES["早餐"][:2]):
            candidates.append(((at - timedelta(days=1)).date().isoformat(), "睡前"))
        with self.conn:
            for med_date, slot in candidates:
                rows = self.conn.execute(
                    "SELECT bc.*, b.resident_id FROM box_cells bc"
                    " JOIN boxes b ON b.id=bc.box_id"
                    " WHERE b.med_date=? AND bc.slot=?"
                    " AND bc.status IN ('PENDING','SCANNED_OK')",
                    (med_date, slot),
                ).fetchall()
                for c in rows:
                    self.conn.execute(
                        "UPDATE box_cells SET status='MISSED' WHERE id=?", (c["id"],)
                    )
                    self._open_exc_locked(
                        "MISSED", "CELL", c["id"], c["resident_id"], self._system_id(),
                        f"漏签：{med_date} {slot} {c['med_name']} 超过 {MISSED_AFTER_MIN} 分钟未签收",
                    )
                    self.conn.execute(
                        "INSERT INTO reminders(kind,ref,message,at) VALUES (?,?,?,?)",
                        ("MISSED", f"cell:{c['id']}",
                         f"漏签提醒 床位{c['bed_no']} {slot} {c['med_name']}", now_iso()),
                    )
                    count += 1
        return count

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def box_detail(self, box_id: int) -> dict:
        box = self.conn.execute(
            "SELECT b.*, r.name resident_name FROM boxes b"
            " JOIN residents r ON r.id=b.resident_id WHERE b.id=?",
            (box_id,),
        ).fetchone()
        if box is None:
            raise RuleViolation("药盒不存在")
        cells = self.conn.execute(
            "SELECT * FROM box_cells WHERE box_id=? ORDER BY position", (box_id,)
        ).fetchall()
        order = self.conn.execute(
            "SELECT * FROM orders WHERE id=?", (box["order_id"],)
        ).fetchone()
        return {"box": dict(box), "order": dict(order),
                "cells": [dict(c) for c in cells]}

    def pending_cells(self, bed_no: str | None = None) -> list[dict]:
        """床旁端：剩余未执行项。"""
        sql = ("SELECT bc.*, r.name resident_name, r.status resident_status,"
               " b.med_date FROM box_cells bc"
               " JOIN boxes b ON b.id=bc.box_id JOIN residents r ON r.id=b.resident_id"
               " WHERE b.status='ISSUED' AND bc.status IN ('SCANNED_OK','REFUSED','DEFERRED')")
        args: list = []
        if bed_no:
            sql += " AND b.bed_no=?"
            args.append(bed_no)
        sql += " ORDER BY b.med_date, bc.position"
        return [dict(r) for r in self.conn.execute(sql, args)]
