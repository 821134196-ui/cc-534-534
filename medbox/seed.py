"""演示数据播种：单条命令启动后即可看到完整签发链与"临时停药"样例。

样例布局（床位/餐次固定）：
  A-101 王秀兰（青霉素过敏）  早餐盒：已完成；晚餐盒：一条暂缓
  A-102 李建国              午餐盒 v1 -> 医嘱临时停用 -> 旧盒阻断/作废 -> v2 重装签发交接
  A-103 孙桂芳（外出中）     装盒/给药被阻断
  A-104 周广田              昨日晚餐盒漏签（提醒任务生成异常）
  A-105 赵玉兰              早餐拒服 -> 异常闭环（药师复核）
"""

from __future__ import annotations

from datetime import timedelta

from .core import MedBoxService, SLOT_LABEL, iso
from .db import connect, init_db
from .photos import save_seal_photo


def seed(conn, photo_dir: str = "data/photos") -> dict:
    svc = MedBoxService(conn)
    scanner = svc.scanner
    today = svc.now().date().isoformat()
    yesterday = (svc.now() - timedelta(days=1)).date().isoformat()

    # ---- 人员 ----
    people = [
        ("u_zhang", "张医生", "physician"),
        ("u_chen", "陈配药", "packer"),
        ("u_li", "李药师", "pharmacist"),
        ("u_wang", "王护", "nurse"),
        ("u_zhao", "赵护", "nurse"),
    ]
    for uid, name, role in people:
        conn.execute("INSERT INTO users(id,name,role) VALUES(?,?,?)", (uid, name, role))

    # ---- 住户 ----
    residents = [
        ("r1", "王秀兰", "A-101", "青霉素"),
        ("r2", "李建国", "A-102", ""),
        ("r3", "孙桂芳", "A-103", ""),
        ("r4", "周广田", "A-104", ""),
        ("r5", "赵玉兰", "A-105", ""),
    ]
    for rid, name, bed, allergy in residents:
        conn.execute(
            "INSERT INTO residents(id,name,bed_no,allergies) VALUES(?,?,?,?)",
            (rid, name, bed, allergy),
        )

    # ---- 医嘱 v1 ----
    svc.new_version("u_zhang", "初版医嘱")
    orders_v1 = [
        # resident, med, spec, dose, slot, qty
        ("r1", "硝苯地平控释片", "30mg", "1片 口服", "breakfast", 1),
        ("r1", "二甲双胍片", "0.5g", "1片 口服", "breakfast", 1),
        ("r1", "阿托伐他汀钙片", "20mg", "1片 睡前", "dinner", 1),
        ("r1", "氢氯噻嗪片", "25mg", "1片 口服", "dinner", 1),
        ("r2", "阿司匹林肠溶片", "100mg", "1片 口服", "lunch", 1),
        ("r2", "氯沙坦钾片", "50mg", "1片 口服", "lunch", 1),
        ("r2", "二甲双胍片", "0.5g", "1片 口服", "breakfast", 1),
        ("r4", "奥美拉唑肠溶胶囊", "20mg", "1粒 口服", "dinner", 1),
        ("r4", "碳酸钙D3片", "600mg", "1片 口服", "dinner", 1),
        ("r5", "格列美脲片", "2mg", "1片 口服", "breakfast", 1),
        ("r5", "缬沙坦胶囊", "80mg", "1粒 口服", "breakfast", 1),
    ]
    order_id = {}
    for rid, med, spec, dose, slot, qty in orders_v1:
        oid = svc.add_order("u_zhang", rid, med, spec, dose, slot, qty)
        order_id[(rid, med)] = oid

    def full_chain(resident, date, slot, results=None):
        """装盒->签发->封盒->交接->（可选）执行。results: {格位号: (结果,原因)}"""
        box = svc.pack_box("u_chen", resident, date, slot)
        svc.sign_box("u_li", box["id"])
        photo = save_seal_photo(photo_dir, box["id"], "u_li", svc.now())
        svc.seal_box("u_li", box["id"], photo)
        svc.handoff("u_wang", box["id"], "u_zhao", "按床位交接，双人核对封签")
        if results:
            for cell, args in results.items():
                item = box["items"][cell - 1]
                code = scanner.encode(item["med_name"], item["spec"])
                result = args[0]
                reason = args[1] if len(args) > 1 else ""
                until = args[2] if len(args) > 2 else None
                svc.administer("u_zhao", item["id"], result, reason=reason,
                               until_time=until, scan_code=code)
        return svc.box_detail(box["id"])

    # ---- A-101 早餐：正常完成 ----
    box_r1_am = full_chain("r1", today, "breakfast", results={
        1: ("taken", ""),
        2: ("taken", ""),
    })

    # ---- A-101 晚餐：一条暂缓（等饭后30分钟） ----
    until = iso(svc.now() + timedelta(minutes=30))
    box_r1_pm = full_chain("r1", today, "dinner", results={
        1: ("deferred", f"住户刚吃完饭，暂缓至 {until}", until),
    })

    # ---- A-102 午餐：临时停药样例（核心演示） ----
    box_r2_lunch = svc.pack_box("u_chen", "r2", today, "lunch")  # v1：含阿司匹林
    stale_id = box_r2_lunch["id"]
    # 医生临时停用阿司匹林 -> 自动生成 v2，v1 即刻失效
    svc.stop_order_temp(
        "u_zhang", [order_id[("r2", "阿司匹林肠溶片")]],
        "牙龈出血，遵医嘱暂停抗血小板药 3 天",
    )
    # 旧版药盒：签发被阻断（STALE_BOX 异常已自动登记）
    # 药师按流程作废旧盒
    svc.void_box("u_li", stale_id,
                 "医嘱已更新至 v2（阿司匹林停用），v1 药盒整盒作废")
    # 按新版本重装 -> 药师核对（只剩氯沙坦）-> 封盒 -> 交接，等待床旁执行
    box_r2_new = svc.pack_box("u_chen", "r2", today, "lunch")
    svc.sign_box("u_li", box_r2_new["id"])
    photo = save_seal_photo(photo_dir, box_r2_new["id"], "u_li", svc.now())
    svc.seal_box("u_li", box_r2_new["id"], photo)
    svc.handoff("u_wang", box_r2_new["id"], "u_zhao", "v2 新盒，旧盒已作废")

    # ---- A-103 外出：装盒阻断 ----
    svc.set_away("u_wang", "r3", True, "家属接回家里住两天", "后天午餐前")

    # ---- A-104 昨日晚餐：漏签样例 ----
    box_r4 = full_chain("r4", yesterday, "dinner", results={1: ("taken", "")})
    # 格位 2（碳酸钙D3）未执行，提醒任务在昨日 19:00 后判定漏签（启动时触发）

    # ---- A-105 早餐：拒服 -> 药师复核闭环 ----
    box_r5 = full_chain("r5", today, "breakfast")
    refused_item = box_r5["items"][0]
    # 另一味药正常签收，使样例聚焦于"格位1拒服闭环"
    other_item = box_r5["items"][1]
    svc.administer("u_zhao", other_item["id"], "taken",
                   scan_code=scanner.encode(other_item["med_name"], other_item["spec"]))
    svc.administer("u_zhao", refused_item["id"], "refused",
                   reason="住户称空腹服药恶心，拒绝服用格列美脲")
    exc = conn.execute(
        "SELECT id FROM exceptions WHERE code='REFUSED' AND status='open' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    svc.close_exception(
        "u_li", exc["id"],
        "已与张医生沟通：改为随第一口饭服用，现场再次发药并确认服下",
    )
    # 复核后重新发药：同格位原记录为 refused 终态，按流程换新盒格位由药师补录执行；
    # 此处登记 admin_event 说明闭环动作，唯一执行记录规则不变。
    conn.execute(
        "INSERT INTO admin_events(item_id,box_id,nurse_id,action,detail,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (refused_item["id"], box_r5["id"], "u_li", "REFUSED_RESOLVED_REDISPENSE",
         "药师复核闭环，医嘱改随餐服用", iso(svc.now())),
    )
    conn.commit()

    return {
        "today": today,
        "stale_box_id": stale_id,
        "new_box_id": box_r2_new["id"],
        "r1_breakfast_box": box_r1_am["id"],
        "r1_dinner_deferred_box": box_r1_pm["id"],
        "r4_missed_box": box_r4["id"],
        "r5_refused_box": box_r5["id"],
        "current_version": svc.current_version()["version_no"],
    }


def ensure_seeded(db_path: str, photo_dir: str = "data/photos", force: bool = False) -> dict:
    conn = connect(db_path)
    init_db(conn)
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if has_users and not force:
        return {"skipped": True}
    if force:
        # 清库重建
        for table in ("notifications","admin_events","exceptions","scans",
                      "administrations","handoffs","box_items","boxes","orders",
                      "order_versions","residents","users"):
            conn.execute(f"DELETE FROM {table}")
    info = seed(conn, photo_dir)
    conn.close()
    return info
