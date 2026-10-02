"""演示数据种子：单条本地命令启动后自动生成。

场景：
1. r1 张建国：v1 医嘱药盒已签发 → 交付 v2（华法林钠片临时停用），旧盒自动作废、
   按 v2 重建今日盒并完成双签与床位交接；
2. r2 孙桂兰：今日盒装盒扫码后，格列美脲片被医嘱临时停用 → 格位阻断、异常 OPEN，
   界面红色提示待药师闭环；
3. r3 周文博：药盒已签发交接，随后住户外出 → 床旁签收被阻断，需双人复核暂缓。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .db import init_db
from .scanner import MockScanner
from .service import MedService


def seed(conn: sqlite3.Connection, med_date: str | None = None,
         scanner: MockScanner | None = None,
         photo_dir: str = "data/photos") -> dict:
    """建演示数据；返回各实体 id 便于测试/演示。已存在数据时直接返回现有 id。"""
    init_db(conn)
    svc = MedService(conn, scanner=scanner or MockScanner(), photo_dir=photo_dir)
    med_date = med_date or date.today().isoformat()

    existing = svc.conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"]
    if existing:
        return _existing_ids(svc, med_date)

    # 员工
    doc = svc.add_staff("D001", "李医生", "DOCTOR")
    pharm_a = svc.add_staff("P001", "王药师", "PHARMACIST")  # 配药人
    pharm_b = svc.add_staff("P002", "赵药师", "PHARMACIST")  # 复核人
    nurse1 = svc.add_staff("N001", "陈护", "NURSE")
    nurse2 = svc.add_staff("N002", "刘护", "NURSE")

    # 住户（床位固定）
    r1 = svc.add_resident("R001", "张建国", "A-101", "101室")
    r2 = svc.add_resident("R002", "孙桂兰", "A-102", "102室")
    r3 = svc.add_resident("R003", "周文博", "A-103", "103室")

    # 禁忌（药师签发时核对）
    svc.add_contraindication(r2, "青霉素V钾片", "青霉素过敏史")

    # ---- v1 医嘱 ----
    o1 = svc.create_order(r1, doc, [
        {"med_name": "华法林钠片", "spec": "2.5mg", "dose_qty": 1,
         "meals": ["早餐", "晚餐"]},
        {"med_name": "二甲双胍片", "spec": "0.5g", "dose_qty": 1,
         "meals": ["早餐", "午餐", "晚餐"]},
    ], note="初始医嘱")
    o2 = svc.create_order(r2, doc, [
        {"med_name": "苯磺酸氨氯地平片", "spec": "5mg", "dose_qty": 1,
         "meals": ["早餐"]},
        {"med_name": "格列美脲片", "spec": "2mg", "dose_qty": 1,
         "meals": ["早餐"]},
    ])
    o3 = svc.create_order(r3, doc, [
        {"med_name": "阿司匹林肠溶片", "spec": "100mg", "dose_qty": 1,
         "meals": ["晚餐"]},
    ])

    # ---- r1：v1 今日盒完成签发（稍后被 v2 作废）----
    old_box = svc.create_box(r1, med_date)
    _pack_and_issue(svc, old_box, pharm_a, pharm_b)
    svc.handover(old_box, pharm_b, nurse1)

    # ---- ★ 停药样例：交付 v2 医嘱 ----
    # 华法林钠片临时停用（新版不再含此药）；二甲双胍剂量与餐次调整；加用他汀。
    o1_v2 = svc.create_order(r1, doc, [
        {"med_name": "二甲双胍片", "spec": "0.5g", "dose_qty": 2,
         "meals": ["早餐", "晚餐"]},
        {"med_name": "阿托伐他汀钙片", "spec": "20mg", "dose_qty": 1,
         "meals": ["睡前"]},
    ], note="华法林钠片临时停用；二甲双胍调整为早晚各2片；加用他汀")
    # old_box 此刻已被自动作废；按 v2 重建今日盒并双签交接
    box1 = svc.create_box(r1, med_date)
    _pack_and_issue(svc, box1, pharm_a, pharm_b)
    svc.handover(box1, pharm_b, nurse1)

    # ---- r2：建盒扫码后，格列美脲片被临时停用 → 格位阻断 ----
    box2 = svc.create_box(r2, med_date)
    for c in svc.box_detail(box2)["cells"]:
        svc.scan_cell(c["id"])
    glime = svc.conn.execute(
        "SELECT id FROM order_items WHERE order_id=? AND med_name='格列美脲片'",
        (o2,),
    ).fetchone()["id"]
    svc.suspend_item(glime, doc)  # 对应格位 BLOCKED，异常 OPEN，box2 无法封盒

    # ---- r3：正常签发交接，随后住户外出 ----
    box3 = svc.create_box(r3, med_date)
    _pack_and_issue(svc, box3, pharm_a, pharm_b)
    svc.handover(box3, pharm_b, nurse1)
    svc.set_resident_out(r3, True)

    return {
        "staff": {"doctor": doc, "pharm_a": pharm_a, "pharm_b": pharm_b,
                  "nurse1": nurse1, "nurse2": nurse2},
        "residents": {"r1": r1, "r2": r2, "r3": r3},
        "orders": {"r1_v1": o1, "r1_v2": o1_v2, "r2": o2, "r3": o3},
        "boxes": {"old_r1_v1": old_box, "today_r1": box1,
                  "today_r2": box2, "today_r3": box3},
        "med_date": med_date,
    }


def _pack_and_issue(svc: MedService, box_id: int, preparer: int, checker: int) -> None:
    for c in svc.box_detail(box_id)["cells"]:
        svc.scan_cell(c["id"])
    svc.pack(box_id, preparer)
    svc.issue(box_id, checker)


def _existing_ids(svc: MedService, med_date: str) -> dict:
    def staff(code: str) -> int:
        return svc.conn.execute(
            "SELECT id FROM staff WHERE code=?", (code,)
        ).fetchone()["id"]

    def resident(code: str) -> int:
        return svc.conn.execute(
            "SELECT id FROM residents WHERE code=?", (code,)
        ).fetchone()["id"]

    def resident_boxes(resident_code: str):
        return [r["id"] for r in svc.conn.execute(
            "SELECT b.id FROM boxes b JOIN residents r ON r.id=b.resident_id"
            " WHERE r.code=? AND b.med_date=? ORDER BY b.id",
            (resident_code, med_date))]

    b1 = resident_boxes("R001")
    return {
        "staff": {"doctor": staff("D001"), "pharm_a": staff("P001"),
                  "pharm_b": staff("P002"), "nurse1": staff("N001"),
                  "nurse2": staff("N002")},
        "residents": {c: resident(c) for c in ("R001", "R002", "R003")},
        "boxes": {"old_r1_v1": b1[0], "today_r1": b1[-1],
                  "today_r2": resident_boxes("R002")[-1],
                  "today_r3": resident_boxes("R003")[-1]},
        "med_date": med_date,
    }
