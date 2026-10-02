"""核心领域服务：所有业务规则与阻断逻辑集中于此。

角色：
  physician  医生 —— 唯一可以开立/停用/修改医嘱的人
  packer     配药员 —— 按医嘱版本装盒
  pharmacist 药师 —— 核对（药名/规格/数量/禁忌）后签发，配药人与复核人必须不同
  nurse      护理员 —— 床位交接、床旁执行；无权改医嘱

阻断 / 复核原则：
  * 医嘱临时停用 -> 旧版本即刻失效，旧版药盒不能签发也不能床旁执行
  * 药片碎裂、错放格位 -> 未处置前阻断签发
  * 住户外出 -> 阻断床旁执行
  * 重复给药 -> 数据库级唯一约束 + 立即阻断（并发安全）
  * 漏签、暂缓到期 -> 进程内提醒任务生成通知与异常
  * 拒服 -> 必须填写原因并进入异常追踪，复核闭环
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .scanner import ScannerAdapter

SLOT_LABEL = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}

# 餐次服药截止时间（本地时间，用于漏签判定）
SLOT_CUTOFF = {"breakfast": "09:30", "lunch": "13:00", "dinner": "19:00"}

ROLE_CAN_PRESCRIBE = {"physician"}
# 药师也可调配装盒；但装盒者本人不得复核签发（见 sign_box 的 SAME_PERSON 阻断）
ROLE_CAN_PACK = {"packer", "pharmacist"}
ROLE_CAN_SIGN = {"pharmacist"}
ROLE_CAN_REVIEW = {"pharmacist", "physician"}
ROLE_BEDSIDE = {"nurse"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    """业务规则被拒绝。code 供前端精确区分提示样式。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MedBoxService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        scanner: Optional[ScannerAdapter] = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.conn = conn
        self.scanner = scanner or ScannerAdapter()
        self.clock = clock

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    def now(self) -> datetime:
        return self.clock()

    def _norm_until(self, value: Optional[str]) -> Optional[str]:
        """把暂缓预计时间统一成内部 ISO UTC。

        接受 ISO 带时区（2026-10-02T12:30:00+00:00）或前端 datetime-local
        产生的本地朴素时间（2026-10-02 12:30 / 2026-10-02T12:30）。
        """
        if not value:
            return None
        text = value.strip().replace(" ", "T")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise DomainError("BAD_TIME", f"时间格式无法解析：{value}")
        if dt.tzinfo is None:
            # 朴素时间按系统本地时区解释
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return iso(dt)

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise DomainError("NO_USER", f"人员不存在：{user_id}")
        return row

    def _require_role(self, user_id: str, roles: set[str], action: str) -> sqlite3.Row:
        user = self._user(user_id)
        if user["role"] not in roles:
            raise DomainError(
                "FORBIDDEN",
                f"{user['name']}（{user['role']}）无权执行「{action}」",
            )
        return user

    def _resident(self, resident_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM residents WHERE id=?", (resident_id,)
        ).fetchone()
        if not row:
            raise DomainError("NO_RESIDENT", f"住户不存在：{resident_id}")
        return row

    def current_version(self) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM order_versions WHERE status='current'"
        ).fetchone()
        if not row:
            raise DomainError("NO_VERSION", "尚无医嘱版本")
        return row

    def _box(self, box_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
        if not row:
            raise DomainError("NO_BOX", f"药盒不存在：{box_id}")
        return row

    def _box_is_current(self, box: sqlite3.Row) -> bool:
        """药盒是否仍与现行医嘱一致。

        版本号升级本身不作废无关住户的药盒；只有该住户该餐次的医嘱内容
        （品项 / 规格 / 剂量 / 数量，含停用）实际发生变化，旧盒才失效。
        """
        current_no = self.conn.execute(
            "SELECT version_no FROM order_versions WHERE status='current'"
        ).fetchone()["version_no"]
        if box["version_no"] == current_no:
            return True
        expected = {o["id"]: o for o in self.active_orders(
            box["resident_id"], box["meal_slot"])}
        items = self.conn.execute(
            "SELECT * FROM box_items WHERE box_id=?", (box["id"],)).fetchall()
        if {i["order_id"] for i in items} != set(expected):
            return False
        for i in items:
            o = expected[i["order_id"]]  # 已停用会落在集合差异里
            if (i["spec"], i["dose_text"]) != (o["spec"], o["dose_text"]):
                return False
            if abs(i["quantity"] - o["quantity"]) > 1e-9:
                return False
        return True

    def _item(self, item_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM box_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise DomainError("NO_ITEM", f"格位不存在：{item_id}")
        return row

    def _event(self, item_id, box_id, nurse_id, action, detail=""):
        self.conn.execute(
            "INSERT INTO admin_events(item_id,box_id,nurse_id,action,detail,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (item_id, box_id, nurse_id, action, detail, iso(self.now())),
        )

    def _open_exception(
        self, code, level, detail, *, box_id=None, item_id=None,
        resident_id=None, admin_id=None, created_by=None,
    ) -> int:
        """同一 (code, box/item) 只保留一条 open 异常，避免重复刷屏。"""
        q = "SELECT id FROM exceptions WHERE status='open' AND code=? AND "
        params: list = [code]
        if item_id is not None:
            q += "item_id IS ?"; params.append(item_id)
        elif box_id is not None:
            q += "box_id IS ? AND item_id IS NULL"; params.append(box_id)
        else:
            q += "box_id IS NULL AND item_id IS NULL"
        existing = self.conn.execute(q, params).fetchone()
        if existing:
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO exceptions(code,level,box_id,item_id,admin_id,resident_id,"
            "status,detail,created_by,created_at) VALUES(?,?,?,?,?,?, 'open',?,?,?)",
            (code, level, box_id, item_id, admin_id, resident_id, detail, created_by,
             iso(self.now())),
        )
        return int(cur.lastrowid)

    def _notify(self, kind, message, box_id=None):
        self.conn.execute(
            "INSERT INTO notifications(kind,message,box_id,created_at) VALUES(?,?,?,?)",
            (kind, message, box_id, iso(self.now())),
        )

    # ------------------------------------------------------------------ #
    # 医嘱版本（只有医生可写）
    # ------------------------------------------------------------------ #
    def new_version(self, actor_id: str, note: str = "") -> int:
        actor = self._require_role(actor_id, ROLE_CAN_PRESCRIBE, "开立医嘱版本")
        self.conn.execute(
            "UPDATE order_versions SET status='superseded' WHERE status='current'"
        )
        cur = self.conn.execute(
            "INSERT INTO order_versions(version_no,status,note,created_by,created_at)"
            " VALUES(COALESCE((SELECT MAX(version_no)+1 FROM order_versions),1),"
            " 'current',?,?,?)",
            (note, actor["id"], iso(self.now())),
        )
        self.conn.commit()
        return self.current_version()["version_no"]

    def add_order(
        self, actor_id: str, resident_id: str, med_name: str, spec: str,
        dose_text: str, meal_slot: str, quantity: float = 1.0,
    ) -> int:
        self._require_role(actor_id, ROLE_CAN_PRESCRIBE, "新增医嘱")
        self._resident(resident_id)
        if meal_slot not in SLOT_LABEL:
            raise DomainError("BAD_SLOT", f"餐次非法：{meal_slot}")
        version_no = self.current_version()["version_no"]
        cur = self.conn.execute(
            "INSERT INTO orders(resident_id,med_name,spec,dose_text,meal_slot,"
            "quantity,version_no,status,created_at) VALUES(?,?,?,?,?,?,?, 'active',?)",
            (resident_id, med_name, spec, dose_text, meal_slot, quantity,
             version_no, iso(self.now())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def stop_order(self, actor_id: str, order_id: int, reason: str) -> None:
        """临时停用：必须在新版本内操作，旧版本即刻失效。"""
        actor = self._require_role(actor_id, ROLE_CAN_PRESCRIBE, "停用医嘱")
        order = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise DomainError("NO_ORDER", f"医嘱不存在：{order_id}")
        if order["status"] == "stopped":
            raise DomainError("ALREADY_STOPPED", f"《{order['med_name']}》已停用")
        version_no = self.current_version()["version_no"]
        if version_no == order["version_no"]:
            raise DomainError(
                "NEED_NEW_VERSION",
                "停用医嘱必须先生成新版本（旧版本即刻封存）",
            )
        self.conn.execute(
            "UPDATE orders SET status='stopped',stop_reason=?,stop_version_no=? WHERE id=?",
            (reason or "医生临时停用", version_no, order_id),
        )

    def stop_order_temp(self, actor_id: str, order_ids: list[int], reason: str) -> int:
        """便捷操作：生成新版本并批量临时停用，返回新版本号。"""
        self._require_role(actor_id, ROLE_CAN_PRESCRIBE, "临时停用医嘱")
        version_no = self.new_version(actor_id, f"临时停用 {len(order_ids)} 条：{reason}")
        for oid in order_ids:
            self.stop_order(actor_id, oid, reason)
        self.conn.commit()
        return version_no

    def active_orders(self, resident_id: str, meal_slot: str) -> list[sqlite3.Row]:
        version_no = self.current_version()["version_no"]
        return list(self.conn.execute(
            "SELECT * FROM orders WHERE resident_id=? AND meal_slot=? AND status='active'"
            " ORDER BY med_name",
            (resident_id, meal_slot),
        ))

    # ------------------------------------------------------------------ #
    # 住户状态（外出 / 返回）
    # ------------------------------------------------------------------ #
    def set_away(self, actor_id: str, resident_id: str, away: bool,
                 reason: str = "", expected_return: str = "") -> None:
        self._require_role(actor_id, {"nurse", "physician"}, "登记外出")
        self._resident(resident_id)
        self.conn.execute(
            "UPDATE residents SET away=?, away_reason=?, expected_return=? WHERE id=?",
            (1 if away else 0, reason, expected_return, resident_id),
        )
        if away:
            self._open_exception(
                "RESIDENT_AWAY", "warn",
                f"住户外出：{reason}（预计返回 {expected_return or '未定'}）",
                resident_id=resident_id, created_by=actor_id,
            )
        else:
            # 返回：闭环该住户所有开放的外出异常
            rows = self.conn.execute(
                "SELECT id FROM exceptions WHERE code='RESIDENT_AWAY' "
                "AND status='open' AND resident_id=?", (resident_id,)).fetchall()
            user = self._user(actor_id)
            for r in rows:
                self.conn.execute(
                    "UPDATE exceptions SET status='closed', resolution=?, resolved_by=?, "
                    "closed_at=? WHERE id=?",
                    (f"[{user['name']}] 住户已返回，恢复发药流程", actor_id,
                     iso(self.now()), r["id"]),
                )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 装盒（配药员）
    # ------------------------------------------------------------------ #
    def pack_box(self, actor_id: str, resident_id: str, serve_date: str,
                 meal_slot: str) -> dict[str, Any]:
        actor = self._require_role(actor_id, ROLE_CAN_PACK, "装盒")
        resident = self._resident(resident_id)
        if resident["away"]:
            self._open_exception(
                "RESIDENT_AWAY", "high",
                f"住户 {resident['name']} 外出中，不能为其装盒：{resident['away_reason']}",
                resident_id=resident_id, created_by=actor_id,
            )
            self._event(None, None, actor_id, "PACK_BLOCKED_AWAY", resident_id)
            self.conn.commit()
            raise DomainError(
                "RESIDENT_AWAY",
                f"住户 {resident['name']} 外出未归（{resident['away_reason']}），已阻断装盒",
            )

        version_no = self.current_version()["version_no"]
        orders = self.active_orders(resident_id, meal_slot)
        if not orders:
            raise DomainError(
                "NO_ACTIVE_ORDER",
                f"{resident['name']} 当前版本无{SLOT_LABEL[meal_slot]}有效医嘱，不能装盒",
            )

        old = self.conn.execute(
            "SELECT * FROM boxes WHERE resident_id=? AND serve_date=? AND meal_slot=? "
            "AND status<>'void' ORDER BY id",
            (resident_id, serve_date, meal_slot),
        ).fetchone()
        if old:
            if self._box_is_current(old):
                raise DomainError(
                    "BOX_EXISTS",
                    f"该住户 {serve_date} {SLOT_LABEL[meal_slot]} 已有未完成药盒 #{old['id']}",
                )
            # 该住户该餐次医嘱已变更：旧盒阻断，要求先按流程作废
            version_no = self.current_version()["version_no"]
            self._open_exception(
                "STALE_BOX", "high",
                f"药盒 #{old['id']} 基于已失效医嘱（v{old['version_no']}），"
                f"当前为 v{version_no}，禁止继续使用，需药师/配药员作废后重装",
                box_id=old["id"], created_by=actor_id,
            )
            self._event(None, old["id"], actor_id, "PACK_BLOCKED_STALE")
            self.conn.commit()
            raise DomainError(
                "STALE_BOX",
                f"存在失效旧盒 #{old['id']}（v{old['version_no']}），"
                f"必须先作废（当前医嘱版本 v{version_no}）",
            )

        cur = self.conn.execute(
            "INSERT INTO boxes(version_no,resident_id,bed_no,meal_slot,serve_date,"
            "status,packed_by,created_at) VALUES(?,?,?,?,?, 'packed',?,?)",
            (version_no, resident_id, resident["bed_no"], meal_slot, serve_date,
             actor["id"], iso(self.now())),
        )
        box_id = int(cur.lastrowid)
        for cell_no, order in enumerate(orders, start=1):
            self.conn.execute(
                "INSERT INTO box_items(box_id,cell_no,order_id,med_name,spec,dose_text,"
                "quantity,state) VALUES(?,?,?,?,?,?,?, 'ok')",
                (box_id, cell_no, order["id"], order["med_name"], order["spec"],
                 order["dose_text"], order["quantity"]),
            )
        self.conn.commit()
        return self.box_detail(box_id)

    def void_box(self, actor_id: str, box_id: int, reason: str) -> None:
        """作废药盒。仅 packed 状态可由配药员作废；其余须药师。"""
        actor = self._user(actor_id)
        box = self._box(box_id)
        if box["status"] == "void":
            raise DomainError("ALREADY_VOID", f"药盒 #{box_id} 已作废")
        if box["status"] == "packed":
            self._require_role(actor_id, ROLE_CAN_PACK | ROLE_CAN_SIGN, "作废药盒")
        else:
            self._require_role(actor_id, ROLE_CAN_SIGN, "作废已流转药盒")
        self.conn.execute(
            "UPDATE boxes SET status='void', void_reason=? WHERE id=?",
            (f"[{actor['name']}] {reason}", box_id),
        )
        # 留一条已闭环的异常作为审计
        self.conn.execute(
            "INSERT INTO exceptions(code,level,box_id,resident_id,status,detail,"
            "resolution,created_by,resolved_by,created_at,closed_at) "
            "VALUES('BOX_VOID','warn',?,?,'closed',?,?,?,?,?,?)",
            (box_id, box["resident_id"], f"药盒 #{box_id} 作废：{reason}",
             f"[{actor['name']}] {reason}", actor["id"], actor["id"],
             iso(self.now()), iso(self.now())),
        )
        self._event(None, box_id, actor_id, "BOX_VOID", reason)
        self.conn.commit()

    def flag_item(self, actor_id: str, item_id: int, state: str, note: str = "") -> None:
        """药片碎裂 / 错放格位登记（装盒环节异常）。"""
        actor = self._require_role(actor_id, ROLE_CAN_PACK | ROLE_CAN_SIGN, "登记格位异常")
        if state not in ("broken", "misplaced"):
            raise DomainError("BAD_STATE", "仅可登记 broken / misplaced")
        item = self._item(item_id)
        self.conn.execute("UPDATE box_items SET state=? WHERE id=?", (state, item_id))
        code = "BROKEN_PILL" if state == "broken" else "MISPLACED_CELL"
        label = "药片碎裂" if state == "broken" else "错放格位"
        self._open_exception(
            code, "high",
            f"药盒 #{item['box_id']} 格位 {item['cell_no']} "
            f"《{item['med_name']}》{label}：{note or '待复核处置'}",
            box_id=item["box_id"], item_id=item_id, created_by=actor_id,
        )
        self.conn.commit()

    def resolve_item(self, actor_id: str, item_id: int, new_cell_no: Optional[int],
                     note: str) -> None:
        """碎药补换 / 错格归位：恢复格位并闭环异常（需药师复核或配药员更正）。"""
        actor = self._require_role(actor_id, ROLE_CAN_PACK | ROLE_CAN_SIGN, "处置格位异常")
        item = self._item(item_id)
        if new_cell_no is not None:
            clash = self.conn.execute(
                "SELECT id FROM box_items WHERE box_id=? AND cell_no=? AND id<>?",
                (item["box_id"], new_cell_no, item_id),
            ).fetchone()
            if clash:
                raise DomainError(
                    "CELL_TAKEN", f"格位 {new_cell_no} 已被占用，固定格位不得重叠")
        self.conn.execute(
            "UPDATE box_items SET state=?, cell_no=COALESCE(?,cell_no) WHERE id=?",
            ("replaced" if item["state"] == "broken" else "ok", new_cell_no, item_id),
        )
        exc = self.conn.execute(
            "SELECT id FROM exceptions WHERE item_id=? AND status='open'", (item_id,)
        ).fetchone()
        if exc:
            self.conn.execute(
                "UPDATE exceptions SET status='closed', resolution=?, resolved_by=?, "
                "closed_at=? WHERE id=?",
                (f"[{actor['name']}] {note}", actor["id"], iso(self.now()), exc["id"]),
            )
        self._event(item_id, item["box_id"], actor_id, "ITEM_RESOLVED", note)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 药师核对签发（配药人与复核人必须不同）
    # ------------------------------------------------------------------ #
    def _contraindications(self, resident: sqlite3.Row, med_name: str) -> Optional[str]:
        allergies = [a.strip() for a in (resident["allergies"] or "").replace("，", ",").split(",") if a.strip()]
        penicillin_drugs = ("青霉素", "阿莫西林", "氨苄西林", "哌拉西林", "美洛西林")
        for allergy in allergies:
            if allergy in med_name:
                return f"过敏禁忌：住户对「{allergy}」过敏，药品含该成分"
            if allergy == "青霉素" and any(k in med_name for k in penicillin_drugs):
                return f"过敏禁忌：青霉素过敏住户禁用《{med_name}》"
        return None

    def sign_box(self, actor_id: str, box_id: int) -> None:
        actor = self._require_role(actor_id, ROLE_CAN_SIGN, "复核签发")
        box = self._box(box_id)
        if box["status"] != "packed":
            raise DomainError("BAD_BOX_STATE",
                              f"药盒 #{box_id} 当前状态 {box['status']}，不能签发")
        if box["packed_by"] == actor["id"]:
            raise DomainError(
                "SAME_PERSON",
                "配药人与复核人必须为不同人员，不能由装盒者本人签发",
            )

        # 版本核对：该住户该餐次医嘱内容若已变更，旧版药盒不能继续使用
        if not self._box_is_current(box):
            current = self.current_version()["version_no"]
            self._open_exception(
                "STALE_BOX", "high",
                f"签发被阻断：药盒 #{box_id} 基于 v{box['version_no']}，"
                f"该住户该餐次医嘱已变更（当前版本 v{current}）",
                box_id=box_id, created_by=actor_id,
            )
            self._event(None, box_id, actor_id, "SIGN_BLOCKED_STALE")
            self.conn.commit()
            raise DomainError(
                "STALE_BOX",
                f"医嘱已变更（药盒 v{box['version_no']} → 当前 v{current}），禁止签发",
            )

        resident = self._resident(box["resident_id"])
        expected = self.active_orders(resident["id"], box["meal_slot"])
        items = self.conn.execute(
            "SELECT * FROM box_items WHERE box_id=? ORDER BY cell_no", (box_id,)
        ).fetchall()

        # 数量/品项一致性
        exp_ids = {o["id"] for o in expected}
        got_ids = {i["order_id"] for i in items}
        if exp_ids != got_ids:
            missing = [o["med_name"] for o in expected if o["id"] not in got_ids]
            extra = [i["med_name"] for i in items if i["order_id"] not in exp_ids]
            detail = f"药盒与当前医嘱不符。缺少：{missing or '无'}；多余：{extra or '无'}"
            self._open_exception("CONTENT_MISMATCH", "high", detail,
                                 box_id=box_id, created_by=actor_id)
            self.conn.commit()
            raise DomainError("CONTENT_MISMATCH", detail)

        qty_map = {o["id"]: o for o in expected}
        for item in items:
            order = qty_map[item["order_id"]]
            if abs(item["quantity"] - order["quantity"]) > 1e-9:
                raise DomainError(
                    "QTY_MISMATCH",
                    f"格位 {item['cell_no']}《{item['med_name']}》数量不符："
                    f"盒内 {item['quantity']} vs 医嘱 {order['quantity']}",
                )
            if item["spec"] != order["spec"] or item["dose_text"] != order["dose_text"]:
                raise DomainError(
                    "SPEC_MISMATCH",
                    f"格位 {item['cell_no']}《{item['med_name']}》规格/剂量与医嘱不符",
                )
            if item["state"] == "broken":
                raise DomainError(
                    "BROKEN_PILL",
                    f"格位 {item['cell_no']}《{item['med_name']}》药片碎裂未补换，禁止签发",
                )
            if item["state"] == "misplaced":
                raise DomainError(
                    "MISPLACED_CELL",
                    f"格位 {item['cell_no']}《{item['med_name']}》错放格位未归位，禁止签发",
                )
            hit = self._contraindications(resident, item["med_name"])
            if hit:
                self._open_exception(
                    "CONTRAINDICATION", "high",
                    f"药盒 #{box_id}《{item['med_name']}》{hit}",
                    box_id=box_id, item_id=item["id"], created_by=actor_id,
                )
                self._event(item["id"], box_id, actor_id, "SIGN_BLOCKED_CONTRA")
                self.conn.commit()
                raise DomainError("CONTRAINDICATION", hit)

        self.conn.execute(
            "UPDATE boxes SET checked_by=?, status='signed', signed_at=? WHERE id=?",
            (actor["id"], iso(self.now()), box_id),
        )
        self._event(None, box_id, actor_id, "BOX_SIGNED")
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 封盒照片（本地存储）
    # ------------------------------------------------------------------ #
    def seal_box(self, actor_id: str, box_id: int, photo_path: str) -> None:
        actor = self._require_role(actor_id, ROLE_CAN_PACK | ROLE_CAN_SIGN, "封盒")
        box = self._box(box_id)
        if box["status"] != "signed":
            raise DomainError("NOT_SIGNED", "仅已签发药盒可以封盒")
        self.conn.execute(
            "UPDATE boxes SET status='sealed', sealed_photo=?, sealed_at=? WHERE id=?",
            (photo_path, iso(self.now()), box_id),
        )
        self._event(None, box_id, actor_id, "BOX_SEALED", photo_path)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 床位交接
    # ------------------------------------------------------------------ #
    def handoff(self, actor_id: str, box_id: int, to_nurse_id: str,
                note: str = "") -> None:
        actor = self._require_role(actor_id, ROLE_BEDSIDE, "床位交接")
        to = self._require_role(to_nurse_id, ROLE_BEDSIDE, "床位交接")
        box = self._box(box_id)
        if box["status"] not in ("sealed", "handed_over"):
            raise DomainError("NOT_SEALED", "仅已封盒药盒可以交接")
        self.conn.execute(
            "INSERT INTO handoffs(box_id,from_nurse,to_nurse,note,created_at)"
            " VALUES(?,?,?,?,?)",
            (box_id, actor["id"], to["id"], note, iso(self.now())),
        )
        self.conn.execute(
            "UPDATE boxes SET status='handed_over', handed_at=? WHERE id=?",
            (iso(self.now()), box_id),
        )
        self._event(None, box_id, actor_id, "HANDOFF", f"→{to['name']} {note}")
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 扫码（模拟适配器）
    # ------------------------------------------------------------------ #
    def scan(self, actor_id: str, code: str) -> dict[str, Any]:
        actor = self._require_role(actor_id, ROLE_BEDSIDE | ROLE_CAN_PACK, "扫码")
        parsed = self.scanner.read(code)
        med = parsed.get("med_name") if parsed else None
        matched = 0
        if med:
            matched = self.conn.execute(
                "SELECT 1 FROM box_items WHERE med_name=? LIMIT 1", (med,)
            ).fetchone() is not None
        self.conn.execute(
            "INSERT INTO scans(nurse_id,raw_code,parsed_med,matched,created_at)"
            " VALUES(?,?,?,?,?)",
            (actor["id"], code, med, 1 if matched else 0, iso(self.now())),
        )
        self.conn.commit()
        return {"raw": code, "med_name": med, "matched": bool(matched)}

    # ------------------------------------------------------------------ #
    # 床旁执行
    # ------------------------------------------------------------------ #
    def administer(self, actor_id: str, item_id: int, result: str,
                   reason: str = "", until_time: Optional[str] = None,
                   scan_code: Optional[str] = None) -> int:
        actor = self._require_role(actor_id, ROLE_BEDSIDE, "床旁给药")
        item = self._item(item_id)
        box = self._box(item["box_id"])

        if box["status"] == "void":
            raise DomainError("BOX_VOID", f"药盒 #{box['id']} 已作废，禁止执行")
        if box["status"] != "handed_over":
            raise DomainError("NOT_HANDED", "药盒尚未完成床位交接，不能床旁执行")
        if not self._box_is_current(box):
            current = self.current_version()["version_no"]
            self._open_exception(
                "STALE_BOX", "high",
                f"床旁执行被阻断：药盒 #{box['id']} 为失效医嘱（v{box['version_no']}），"
                f"该住户该餐次已变更（当前 v{current}）",
                box_id=box["id"], item_id=item_id, created_by=actor_id,
            )
            self._event(item_id, box["id"], actor_id, "ADMIN_BLOCKED_STALE")
            self.conn.commit()
            raise DomainError("STALE_BOX", "失效医嘱药盒不能继续使用，已阻断执行")

        resident = self._resident(box["resident_id"])
        if resident["away"]:
            self._open_exception(
                "RESIDENT_AWAY", "high",
                f"床旁执行被阻断：{resident['name']} 外出未归（{resident['away_reason']}）",
                box_id=box["id"], item_id=item_id, resident_id=resident["id"],
                created_by=actor_id,
            )
            self._event(item_id, box["id"], actor_id, "ADMIN_BLOCKED_AWAY")
            self.conn.commit()
            raise DomainError("RESIDENT_AWAY",
                              f"住户外出未归（{resident['away_reason']}），已阻断给药并待复核")

        if item["state"] == "broken":
            raise DomainError("BROKEN_PILL", "该药格药片碎裂，须更换后再执行")
        if item["state"] == "misplaced":
            raise DomainError("MISPLACED_CELL", "该药格错放未归位，须复核后再执行")

        if result not in ("taken", "refused", "deferred"):
            raise DomainError("BAD_RESULT", "执行结果只能是 taken/refused/deferred")
        if result in ("refused", "deferred") and not reason.strip():
            raise DomainError("REASON_REQUIRED",
                              "拒服或暂缓必须记录原因")
        if result == "deferred" and not until_time:
            raise DomainError("UNTIL_REQUIRED", "暂缓必须填写预计执行时间")
        if until_time:
            until_time = self._norm_until(until_time)

        if result == "taken":
            # 扫码核对：模拟适配器读数必须与格位药品一致
            if scan_code:
                parsed = self.scanner.read(scan_code)
                med = parsed.get("med_name") if parsed else None
                if med != item["med_name"]:
                    self._event(item_id, box["id"], actor_id, "ADMIN_BLOCKED_SCAN",
                                f"scan={scan_code}")
                    self.conn.commit()
                    raise DomainError(
                        "SCAN_MISMATCH",
                        f"扫码药品「{med}」与格位药品「{item['med_name']}」不一致，已阻断",
                    )

        # 重复给药：每个格位全局唯一有效执行记录（并发安全）
        # taken/refused 为终态，任何再次确认一律阻断；
        # deferred 可由后续跟进更新（暂缓->已服/拒服，仍只有一条记录）。
        existing = self.conn.execute(
            "SELECT a.* FROM administrations a WHERE a.item_id=?", (item_id,)
        ).fetchone()
        if existing and existing["result"] in ("taken", "refused"):
            self._event(item_id, box["id"], actor_id, "DUPLICATE_BLOCKED",
                        f"existing={existing['result']} attempted={result}")
            self._open_exception(
                "DUPLICATE_ADMIN", "warn",
                f"格位 {item['cell_no']}《{item['med_name']}》出现重复给药尝试"
                f"（已有记录：{existing['result']}，操作人 {actor['name']}），已阻断待复核",
                box_id=box["id"], item_id=item_id, created_by=actor_id,
            )
            self.conn.commit()
            raise DomainError(
                "DUPLICATE_ADMIN",
                f"该格位已有有效执行记录（{existing['result']}），重复给药已阻断",
            )

        ts = iso(self.now())
        try:
            if existing:
                # deferred -> 终态跟进（仍只保留这一条执行记录）
                self.conn.execute(
                    "UPDATE administrations SET result=?, reason=COALESCE(NULLIF(?, ''),"
                    "reason), until_time=?, updated_at=? WHERE id=?",
                    (result, reason, until_time, ts, existing["id"]),
                )
                admin_id = existing["id"]
            else:
                cur = self.conn.execute(
                    "INSERT INTO administrations(item_id,box_id,nurse_id,result,reason,"
                    "until_time,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (item_id, box["id"], actor["id"], result, reason, until_time, ts, ts),
                )
                admin_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # 并发下另一事务抢先写入：唯一约束兜底
            self.conn.rollback()
            self._event(item_id, box["id"], actor_id, "DUPLICATE_BLOCKED", "race-lost")
            self._open_exception(
                "DUPLICATE_ADMIN", "warn",
                f"格位 {item['cell_no']}《{item['med_name']}》并发重复给药被数据库阻断",
                box_id=box["id"], item_id=item_id, created_by=actor_id,
            )
            self.conn.commit()
            raise DomainError("DUPLICATE_ADMIN", "并发确认冲突：该格位已被执行，仅保留一条记录")

        if result == "taken":
            self._close_open_exceptions(item_id, ("DEFERRED",), actor_id,
                                        f"暂缓后已服药（{actor['name']}）")
            self._event(item_id, box["id"], actor_id, "ADMIN_TAKEN")
        elif result == "refused":
            self._close_open_exceptions(item_id, ("DEFERRED",), actor_id,
                                        f"暂缓跟进转为拒服（{actor['name']}）")
            self._open_exception(
                "REFUSED", "high",
                f"{resident['name']} 拒服《{item['med_name']}》：{reason} —— 需医护复核处置",
                box_id=box["id"], item_id=item_id, admin_id=admin_id,
                resident_id=resident["id"], created_by=actor_id,
            )
            self._notify("refused",
                         f"拒服待复核：{resident['name']}《{item['med_name']}》", box["id"])
            self._event(item_id, box["id"], actor_id, "ADMIN_REFUSED", reason)
        else:
            self._open_exception(
                "DEFERRED", "warn",
                f"{resident['name']} 暂缓《{item['med_name']}》至 {until_time}：{reason}",
                box_id=box["id"], item_id=item_id, admin_id=admin_id,
                resident_id=resident["id"], created_by=actor_id,
            )
            self._event(item_id, box["id"], actor_id, "ADMIN_DEFERRED",
                        f"{until_time} {reason}")
        self.conn.commit()
        return admin_id

    def followup_deferred(self, actor_id: str, admin_id: int, final_result: str,
                          reason: str = "", scan_code: Optional[str] = None) -> None:
        """暂缓到期跟进：转为 已服 / 拒服。只能由护理员执行，且仍受扫码校验。"""
        actor = self._require_role(actor_id, ROLE_BEDSIDE, "暂缓跟进")
        admin = self.conn.execute(
            "SELECT * FROM administrations WHERE id=?", (admin_id,)
        ).fetchone()
        if not admin:
            raise DomainError("NO_ADMIN", "执行记录不存在")
        if admin["result"] != "deferred":
            raise DomainError("NOT_DEFERRED", "仅暂缓记录可以跟进")
        item = self._item(admin["item_id"])
        # 复用统一入口，保证版本/外出/扫码/唯一记录等阻断规则一致
        self.administer(actor_id, item["id"], final_result, reason=reason,
                        scan_code=scan_code)

    def _close_open_exceptions(self, item_id, codes, actor, resolution):
        rows = self.conn.execute(
            "SELECT id FROM exceptions WHERE item_id=? AND status='open' AND code IN (%s)"
            % ",".join("?" for _ in codes),
            [item_id, *codes],
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE exceptions SET status='closed', resolution=?, resolved_by=?, "
                "closed_at=? WHERE id=?",
                (resolution, actor["id"] if isinstance(actor, sqlite3.Row) else actor,
                 iso(self.now()), r["id"]),
            )

    def close_exception(self, actor_id: str, exception_id: int, resolution: str) -> None:
        actor = self._require_role(actor_id, ROLE_CAN_REVIEW, "异常复核闭环")
        exc = self.conn.execute("SELECT * FROM exceptions WHERE id=?",
                                (exception_id,)).fetchone()
        if not exc:
            raise DomainError("NO_EXCEPTION", "异常不存在")
        if exc["status"] == "closed":
            raise DomainError("ALREADY_CLOSED", "异常已闭环")
        if not resolution.strip():
            raise DomainError("RESOLUTION_REQUIRED", "闭环必须填写复核处置说明")
        self.conn.execute(
            "UPDATE exceptions SET status='closed', resolution=?, resolved_by=?, "
            "closed_at=? WHERE id=?",
            (f"[{actor['name']}] {resolution}", actor["id"], iso(self.now()),
             exception_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 进程内提醒任务：暂缓到期 / 漏签
    # ------------------------------------------------------------------ #
    def run_reminders(self, now: Optional[datetime] = None) -> dict[str, int]:
        now = now or self.now()
        due_deferred = 0
        missed = 0

        # 1) 暂缓到期
        rows = self.conn.execute(
            "SELECT a.*, r.name AS rname, i.med_name FROM administrations a"
            " JOIN box_items i ON i.id=a.item_id"
            " JOIN boxes b ON b.id=a.box_id"
            " JOIN residents r ON r.id=b.resident_id"
            " WHERE a.result='deferred' AND a.until_time<=?",
            (iso(now),),
        ).fetchall()
        for r in rows:
            exists = self.conn.execute(
                "SELECT 1 FROM notifications WHERE kind='deferred_due' AND box_id=? "
                "AND message LIKE ?",
                (r["box_id"], f"%{r['med_name']}%"),
            ).fetchone()
            if not exists:
                self._notify(
                    "deferred_due",
                    f"暂缓到期：{r['rname']}《{r['med_name']}》应于 {r['until_time']} 补服，请跟进",
                    r["box_id"],
                )
                due_deferred += 1

        # 2) 漏签：已到床旁（handed_over）且超过餐次截止时间，仍有格位无执行记录
        boxes = self.conn.execute(
            "SELECT b.*, r.name AS rname FROM boxes b JOIN residents r ON r.id=b.resident_id"
            " WHERE b.status='handed_over'"
        ).fetchall()
        for b in boxes:
            hh, mm = map(int, SLOT_CUTOFF[b["meal_slot"]].split(":"))
            try:
                y, m, d = map(int, b["serve_date"].split("-"))
                cutoff = datetime(y, m, d, hh, mm, tzinfo=now.tzinfo)
            except ValueError:
                continue
            if now < cutoff:
                continue
            # 漏签：超过餐次截止时间仍完全没有执行记录（拒服/暂缓已有记录，不算漏签）
            pending = self.conn.execute(
                "SELECT i.* FROM box_items i WHERE i.box_id=? AND NOT EXISTS("
                "SELECT 1 FROM administrations a WHERE a.item_id=i.id)",
                (b["id"],),
            ).fetchall()
            if pending:
                exc_id = self._open_exception(
                    "MISSED_SIGN", "warn",
                    f"{b['serve_date']} {SLOT_LABEL[b['meal_slot']]} 药盒 #{b['id']}"
                    f"（{b['rname']}）有 {len(pending)} 个格位超过截止时间未签收",
                    box_id=b["id"],
                )
                notified = self.conn.execute(
                    "SELECT 1 FROM notifications WHERE kind='missed_sign' AND box_id=?",
                    (b["id"],),
                ).fetchone()
                if not notified:
                    self._notify(
                        "missed_sign",
                        f"漏签提醒：{b['rname']} {SLOT_LABEL[b['meal_slot']]} "
                        f"{len(pending)} 格未执行（异常 #{exc_id}）",
                        b["id"],
                    )
                    missed += 1
        self.conn.commit()
        return {"deferred_due": due_deferred, "missed_sign": missed}

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def box_detail(self, box_id: int) -> dict[str, Any]:
        box = dict(self._box(box_id))
        resident = self.conn.execute(
            "SELECT * FROM residents WHERE id=?", (box["resident_id"],)
        ).fetchone()
        items = [dict(r) for r in self.conn.execute(
            "SELECT i.*, a.result AS admin_result, a.reason AS admin_reason,"
            " a.until_time, a.id AS admin_id, a.nurse_id AS admin_nurse"
            " FROM box_items i LEFT JOIN administrations a ON a.item_id=i.id"
            " WHERE i.box_id=? ORDER BY i.cell_no", (box_id,))]
        box["resident_name"] = resident["name"]
        box["meal_label"] = SLOT_LABEL[box["meal_slot"]]
        box["items"] = items
        # 剩余未执行：完全没有任何执行记录的格位（拒服/暂缓已处置，不计入）
        box["remaining"] = sum(1 for i in items if i["admin_result"] is None)
        box["pending_taken"] = sum(1 for i in items if i["admin_result"] != "taken")
        box["stale"] = (box["status"] != "void" and not self._box_is_current(
            self._box(box_id)))
        # 全部格位已签收才自动完成；存在拒服/暂缓时仍留在床旁端待跟进
        if box["status"] == "handed_over" and items and box["pending_taken"] == 0:
            self.conn.execute("UPDATE boxes SET status='completed' WHERE id=?", (box_id,))
            self.conn.commit()
            box["status"] = "completed"
        return box

    def board(self, serve_date: str) -> dict[str, Any]:
        users = [dict(r) for r in self.conn.execute("SELECT * FROM users ORDER BY id")]
        residents = [dict(r) for r in self.conn.execute(
            "SELECT * FROM residents ORDER BY bed_no")]
        version = dict(self.current_version())
        orders = [dict(r) for r in self.conn.execute(
            "SELECT o.*, r.name AS resident_name, r.bed_no FROM orders o"
            " JOIN residents r ON r.id=o.resident_id ORDER BY r.bed_no, o.meal_slot, o.id")]
        boxes = [self.box_detail(r["id"]) for r in self.conn.execute(
            "SELECT id FROM boxes WHERE serve_date=? ORDER BY bed_no, meal_slot",
            (serve_date,))]
        # 也带上历史日期未完成的盒子（漏签样例可能在昨天）
        extra = [self.box_detail(r["id"]) for r in self.conn.execute(
            "SELECT id FROM boxes WHERE serve_date<>? AND status IN "
            "('handed_over','sealed','signed','packed') ORDER BY serve_date",
            (serve_date,))]
        for b in extra:
            if b["id"] not in {x["id"] for x in boxes}:
                boxes.append(b)
        exceptions = [dict(r) for r in self.conn.execute(
            "SELECT e.*, r.name AS resident_name FROM exceptions e"
            " LEFT JOIN residents r ON r.id=e.resident_id"
            " ORDER BY CASE e.status WHEN 'open' THEN 0 ELSE 1 END,"
            " CASE e.level WHEN 'high' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, e.id DESC")]
        notifications = [dict(r) for r in self.conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT 30")]
        events = [dict(r) for r in self.conn.execute(
            "SELECT v.*, u.name AS nurse_name FROM admin_events v"
            " LEFT JOIN users u ON u.id=v.nurse_id ORDER BY v.id DESC LIMIT 40")]
        return {
            "users": users, "residents": residents, "version": version,
            "orders": orders, "boxes": boxes, "exceptions": exceptions,
            "notifications": notifications, "events": events,
            "slot_labels": SLOT_LABEL, "date": serve_date,
        }
