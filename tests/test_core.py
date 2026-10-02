"""核心业务规则测试。

重点覆盖需求点名场景：
  * 医嘱版本失效（临时停用 -> 旧盒不可签发/执行，作废后按新版重装）
  * 双人复核（配药人与复核人必须不同；角色强制；护理员无权改医嘱）
  * 重复执行（顺序与并发下同一格位都只有一条有效记录）
  * 拒服闭环（必须填原因 -> 高风险异常 -> 药师复核闭环）
其余阻断：碎药 / 错格 / 外出 / 扫码不符 / 禁忌 / 暂缓跟进 / 漏签提醒。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medbox.core import DomainError, MedBoxService
from medbox.db import connect, init_db

UTC = timezone.utc


class Clock:
    """可控时钟。"""

    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kw):
        self.dt += timedelta(**kw)


class MedBoxTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        self.clock = Clock(datetime(2026, 10, 2, 8, 0, tzinfo=UTC))
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.svc = MedBoxService(self.conn, clock=self.clock)
        self._bootstrap()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _bootstrap(self):
        for uid, name, role in [
            ("doc", "张医生", "physician"),
            ("pck", "陈配药", "packer"),
            ("phm", "李药师", "pharmacist"),
            ("n1", "王护", "nurse"),
            ("n2", "赵护", "nurse"),
        ]:
            self.conn.execute("INSERT INTO users VALUES(?,?,?)", (uid, name, role))
        self.conn.execute(
            "INSERT INTO residents(id,name,bed_no,allergies,away,away_reason,"
            "expected_return) VALUES('r1','王秀兰','A-101','青霉素',0,NULL,NULL)")
        self.conn.execute(
            "INSERT INTO residents(id,name,bed_no,allergies,away,away_reason,"
            "expected_return) VALUES('r2','李建国','A-102','',0,NULL,NULL)")
        self.conn.commit()

    def v1_orders(self):
        self.svc.new_version("doc", "v1")
        aspirin = self.svc.add_order("doc", "r2", "阿司匹林肠溶片", "100mg",
                                     "1片 口服", "lunch", 1)
        losartan = self.svc.add_order("doc", "r2", "氯沙坦钾片", "50mg",
                                      "1片 口服", "lunch", 1)
        amox = self.svc.add_order("doc", "r1", "阿莫西林胶囊", "0.25g",
                                  "2粒 口服", "lunch", 2)
        return aspirin, losartan, amox

    def ready_box(self, resident="r2", slot="lunch", date="2026-10-02"):
        """装盒->药师签发->封盒->交接，返回盒详情。"""
        box = self.svc.pack_box("pck", resident, date, slot)
        self.svc.sign_box("phm", box["id"])
        self.svc.seal_box("phm", box["id"], f"/tmp/seal_{box['id']}.png")
        self.svc.handoff("n1", box["id"], "n2", "交接")
        return self.svc.box_detail(box["id"])


class TestVersionInvalidation(MedBoxTestBase):
    def test_stale_box_cannot_be_signed_or_administered(self):
        aspirin, losartan, _ = self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.assertEqual(2, len(box["items"]))

        # 医生临时停用 -> v2，v1 即刻失效
        v2 = self.svc.stop_order_temp("doc", [aspirin], "牙龈出血")
        self.assertEqual(2, v2)

        # 旧版药盒不能签发
        with self.assertRaises(DomainError) as cm:
            self.svc.sign_box("phm", box["id"])
        self.assertEqual("STALE_BOX", cm.exception.code)

        # 即使误流转（直接改库模拟），床旁端同样阻断
        self.conn.execute(
            "UPDATE boxes SET status='handed_over', checked_by='phm' WHERE id=?",
            (box["id"],))
        self.conn.commit()
        with self.assertRaises(DomainError) as cm2:
            self.svc.administer("n2", box["items"][0]["id"], "taken")
        self.assertEqual("STALE_BOX", cm2.exception.code)

        # 作废旧盒 -> 按 v2 重装：盒内只剩氯沙坦，可正常签发
        self.svc.void_box("phm", box["id"], "v1 作废")
        box2 = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.assertEqual([losartan], [i["order_id"] for i in box2["items"]])
        self.svc.sign_box("phm", box2["id"])  # 不抛异常即通过
        self.assertEqual("signed", self.svc._box(box2["id"])["status"])

    def test_packing_blocked_when_unvoided_stale_exists(self):
        aspirin, _, _ = self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.svc.stop_order_temp("doc", [aspirin], "停")
        with self.assertRaises(DomainError) as cm:
            self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.assertEqual("STALE_BOX", cm.exception.code)

    def test_stop_must_create_new_version(self):
        # 在当前版本内直接停用不允许
        self.v1_orders()
        order = self.conn.execute("SELECT id FROM orders LIMIT 1").fetchone()
        with self.assertRaises(DomainError) as cm:
            self.svc.stop_order("doc", order["id"], "x")
        self.assertEqual("NEED_NEW_VERSION", cm.exception.code)

    def test_unrelated_version_bump_does_not_invalidate_box(self):
        """v2 只改了 r2 的医嘱：r1 自己餐次内容未变，其药盒仍可正常执行。"""
        aspirin, losartan, amox = self.v1_orders()
        # r1 另开一种非禁忌药（硝苯地平·早餐），走完全流程在床旁
        nifed = self.svc.add_order("doc", "r1", "硝苯地平控释片", "30mg",
                                   "1片 口服", "breakfast", 1)
        self.conn.commit()
        box_r1 = self.ready_box(resident="r1", slot="breakfast")
        # r2 停用阿司匹林 -> 全局版本升到 v2
        self.svc.stop_order_temp("doc", [aspirin], "出血停药")
        # r1 早餐盒内容（硝苯地平）未变，不失效，可继续执行
        detail = self.svc.box_detail(box_r1["id"])
        self.assertFalse(detail["stale"])
        self.svc.administer("n2", detail["items"][0]["id"], "taken",
                            scan_code="MED:硝苯地平控释片;30mg;B2026")

    def test_other_resident_box_blocked_when_their_orders_change(self):
        aspirin, losartan, amox = self.v1_orders()
        # r2 的午餐盒已在床旁；随后其阿司匹林被停用
        box_r2 = self.ready_box(resident="r2", slot="lunch")
        self.svc.stop_order_temp("doc", [aspirin], "出血停药")
        detail = self.svc.box_detail(box_r2["id"])
        self.assertTrue(detail["stale"])
        with self.assertRaises(DomainError) as cm:
            self.svc.administer("n2", detail["items"][0]["id"], "taken",
                                scan_code="MED:阿司匹林肠溶片;100mg;B2026")
        self.assertEqual("STALE_BOX", cm.exception.code)


class TestDoubleSign(MedBoxTestBase):
    def test_packer_cannot_sign_own_box(self):
        self.v1_orders()
        # 药师本人调配装盒，再由本人复核 -> 必须换人
        box = self.svc.pack_box("phm", "r2", "2026-10-02", "lunch")
        with self.assertRaises(DomainError) as cm:
            self.svc.sign_box("phm", box["id"])
        self.assertEqual("SAME_PERSON", cm.exception.code)
        # 换一名药师（与装盒人不同）可正常签发：临时加第二个药师
        self.conn.execute("INSERT INTO users VALUES('phm2','周药师','pharmacist')")
        self.conn.commit()
        self.svc.sign_box("phm2", box["id"])
        self.assertEqual("phm2", self.svc._box(box["id"])["checked_by"])

    def test_nurse_and_packer_cannot_sign(self):
        self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        with self.assertRaises(DomainError) as cm:
            self.svc.sign_box("n2", box["id"])
        self.assertEqual("FORBIDDEN", cm.exception.code)

    def test_other_pharmacist_signs_and_chain_is_recorded(self):
        self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.svc.sign_box("phm", box["id"])
        row = self.svc._box(box["id"])
        self.assertEqual("packed_by=pck", f"packed_by={row['packed_by']}")
        self.assertEqual("phm", row["checked_by"])
        self.assertNotEqual(row["packed_by"], row["checked_by"])

    def test_contraindication_blocks_sign(self):
        _, _, amox = self.v1_orders()
        box = self.svc.pack_box("pck", "r1", "2026-10-02", "lunch")
        with self.assertRaises(DomainError) as cm:
            self.svc.sign_box("phm", box["id"])
        self.assertEqual("CONTRAINDICATION", cm.exception.code)
        # 禁忌已登记为高风险异常
        exc = self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE code='CONTRAINDICATION' "
            "AND status='open' AND level='high'").fetchone()
        self.assertEqual(1, exc["c"])

    def test_nurse_cannot_prescribe(self):
        self.v1_orders()
        with self.assertRaises(DomainError) as cm:
            self.svc.new_version("n1", "护理员想开医嘱")
        self.assertEqual("FORBIDDEN", cm.exception.code)
        with self.assertRaises(DomainError) as cm2:
            self.svc.add_order("n1", "r2", "X", "1mg", "1片", "lunch")
        self.assertEqual("FORBIDDEN", cm2.exception.code)


class TestDuplicateAdministration(MedBoxTestBase):
    def _handed_box_with_item(self):
        self.v1_orders()
        box = self.ready_box()
        item = box["items"][0]
        code = f"MED:{item['med_name']};{item['spec']};B2026"
        return box, item, code

    def test_second_taken_is_blocked_with_single_record(self):
        _, item, code = self._handed_box_with_item()
        self.svc.administer("n2", item["id"], "taken", scan_code=code)
        with self.assertRaises(DomainError) as cm:
            self.svc.administer("n2", item["id"], "taken", scan_code=code)
        self.assertEqual("DUPLICATE_ADMIN", cm.exception.code)
        # 另一护理员同样被阻断
        with self.assertRaises(DomainError):
            self.svc.administer("n1", item["id"], "taken", scan_code=code)
        count = self.conn.execute(
            "SELECT COUNT(*) c FROM administrations WHERE item_id=?",
            (item["id"],)).fetchone()["c"]
        self.assertEqual(1, count)
        # 重复尝试全部留痕
        blocked = self.conn.execute(
            "SELECT COUNT(*) c FROM admin_events WHERE action='DUPLICATE_BLOCKED'"
        ).fetchone()["c"]
        self.assertGreaterEqual(blocked, 2)

    def test_concurrent_double_confirm_leaves_one_record(self):
        """两人同时确认同一服药格：数据库唯一约束兜底，只留一条有效记录。"""
        _, item, code = self._handed_box_with_item()
        item_id = item["id"]
        errors = []
        barrier = threading.Barrier(2)

        def worker(nurse):
            try:
                conn = connect(self.db_path)
                svc = MedBoxService(conn, clock=self.clock)
                barrier.wait(timeout=10)
                svc.administer(nurse, item_id, "taken", scan_code=code)
                conn.close()
            except DomainError as e:
                errors.append(e.code)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        t1 = threading.Thread(target=worker, args=("n1",))
        t2 = threading.Thread(target=worker, args=("n2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        rows = self.conn.execute(
            "SELECT * FROM administrations WHERE item_id=?", (item_id,)).fetchall()
        self.assertEqual(1, len(rows), "同一格位只能有一条执行记录")
        self.assertEqual(1, len(errors))
        self.assertEqual("DUPLICATE_ADMIN", errors[0])

    def test_refused_is_terminal(self):
        _, item, code = self._handed_box_with_item()
        self.svc.administer("n2", item["id"], "refused", reason="住户拒服")
        with self.assertRaises(DomainError) as cm:
            self.svc.administer("n2", item["id"], "taken", scan_code=code)
        self.assertEqual("DUPLICATE_ADMIN", cm.exception.code)

    def test_deferred_can_be_followed_up_once(self):
        _, item, code = self._handed_box_with_item()
        admin_id = self.svc.administer(
            "n2", item["id"], "deferred", reason="饭后再服",
            until_time="2026-10-02T12:30:00+00:00")
        # 跟进：暂缓 -> 已服，仍只有一条记录
        self.svc.followup_deferred("n2", admin_id, "taken", "已补服",
                                   scan_code=code)
        rows = self.conn.execute(
            "SELECT * FROM administrations WHERE item_id=?", (item["id"],)).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("taken", rows[0]["result"])
        # 终态后不能再次跟进
        with self.assertRaises(DomainError) as cm:
            self.svc.followup_deferred("n2", admin_id, "taken")
        self.assertEqual("NOT_DEFERRED", cm.exception.code)


class TestRefusedLoop(MedBoxTestBase):
    def test_refused_requires_reason_and_opens_high_exception(self):
        self.v1_orders()
        box = self.ready_box()
        item = box["items"][0]
        with self.assertRaises(DomainError) as cm:
            self.svc.administer("n2", item["id"], "refused", reason="  ")
        self.assertEqual("REASON_REQUIRED", cm.exception.code)

        self.svc.administer("n2", item["id"], "refused", reason="恶心拒服")
        exc = self.conn.execute(
            "SELECT * FROM exceptions WHERE code='REFUSED' AND item_id=?",
            (item["id"],)).fetchone()
        self.assertIsNotNone(exc)
        self.assertEqual("open", exc["status"])
        self.assertEqual("high", exc["level"])
        # 同步产生提醒通知
        note = self.conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE kind='refused'").fetchone()
        self.assertEqual(1, note["c"])

    def test_only_reviewer_role_can_close(self):
        self.v1_orders()
        box = self.ready_box()
        item = box["items"][0]
        self.svc.administer("n2", item["id"], "refused", reason="恶心拒服")
        exc = self.conn.execute(
            "SELECT id FROM exceptions WHERE code='REFUSED'").fetchone()

        # 护理员无权复核闭环
        with self.assertRaises(DomainError) as cm:
            self.svc.close_exception("n2", exc["id"], "已处理")
        self.assertEqual("FORBIDDEN", cm.exception.code)
        # 配药员也不行
        with self.assertRaises(DomainError):
            self.svc.close_exception("pck", exc["id"], "已处理")
        # 闭环必须写处置说明
        with self.assertRaises(DomainError) as cm2:
            self.svc.close_exception("phm", exc["id"], " ")
        self.assertEqual("RESOLUTION_REQUIRED", cm2.exception.code)

        # 药师复核闭环
        self.svc.close_exception("phm", exc["id"], "改为随餐服用，已与医生确认")
        after = self.conn.execute("SELECT * FROM exceptions WHERE id=?",
                                  (exc["id"],)).fetchone()
        self.assertEqual("closed", after["status"])
        self.assertEqual("phm", after["resolved_by"])
        self.assertIn("随餐", after["resolution"])


class TestOtherBlocks(MedBoxTestBase):
    def test_broken_and_misplaced_block_sign_until_resolved(self):
        self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.svc.flag_item("pck", box["items"][0]["id"], "broken", "裂片")
        with self.assertRaises(DomainError) as cm:
            self.svc.sign_box("phm", box["id"])
        self.assertEqual("BROKEN_PILL", cm.exception.code)
        self.svc.resolve_item("pck", box["items"][0]["id"], None, "已补换同批号药片")
        self.svc.sign_box("phm", box["id"])

        box = self.svc.box_detail(box["id"])
        # 碎药异常已闭环
        open_exc = self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE code='BROKEN_PILL' "
            "AND status='open'").fetchone()["c"]
        self.assertEqual(0, open_exc)

    def test_misplaced_cell_collision_and_resolve(self):
        self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.svc.flag_item("pck", box["items"][0]["id"], "misplaced", "放到2格")
        # 不能归位到已占用格位
        with self.assertRaises(DomainError) as cm:
            self.svc.resolve_item("pck", box["items"][0]["id"], 2, "归位")
        self.assertEqual("CELL_TAKEN", cm.exception.code)
        self.svc.resolve_item("pck", box["items"][0]["id"], 1, "核对归位")
        self.svc.sign_box("phm", box["id"])

    def test_away_blocks_pack_and_administer(self):
        self.v1_orders()
        self.svc.set_away("n1", "r2", True, "回家住", "10-04")
        with self.assertRaises(DomainError) as cm:
            self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.assertEqual("RESIDENT_AWAY", cm.exception.code)

        # 已在床旁的盒子：外出阻断执行
        self.svc.set_away("n1", "r2", False)
        box = self.ready_box()
        self.svc.set_away("n1", "r2", True, "临时被家属接走", "晚间")
        with self.assertRaises(DomainError) as cm2:
            self.svc.administer("n2", box["items"][0]["id"], "taken")
        self.assertEqual("RESIDENT_AWAY", cm2.exception.code)

    def test_scan_mismatch_blocks(self):
        self.v1_orders()
        box = self.ready_box()
        item = box["items"][0]
        wrong = "MED:氯沙坦钾片;50mg;B2026" if item["med_name"] == "阿司匹林肠溶片" \
            else "MED:阿司匹林肠溶片;100mg;B2026"
        with self.assertRaises(DomainError) as cm:
            self.svc.administer("n2", item["id"], "taken", scan_code=wrong)
        self.assertEqual("SCAN_MISMATCH", cm.exception.code)
        # 阻断不落执行记录
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM administrations WHERE item_id=?", (item["id"],)).fetchone())

    def test_must_hand_over_before_administer(self):
        self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        with self.assertRaises(DomainError) as cm:
            self.svc.administer("n2", box["items"][0]["id"], "taken")
        self.assertEqual("NOT_HANDED", cm.exception.code)

    def test_void_box_cannot_be_used(self):
        self.v1_orders()
        box = self.svc.pack_box("pck", "r2", "2026-10-02", "lunch")
        self.svc.void_box("pck", box["id"], "不要了")
        with self.assertRaises(DomainError) as cm:
            self.svc.sign_box("phm", box["id"])
        self.assertEqual("BAD_BOX_STATE", cm.exception.code)


class TestReminders(MedBoxTestBase):
    def test_missed_sign_after_cutoff(self):
        self.v1_orders()
        # 昨天的盒子，已交接但整盒没执行
        box = self.ready_box(date="2026-10-01")
        # 时间在昨天晚餐截止前（午餐截止 13:00 UTC）-> 无漏签
        self.clock.dt = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        r = self.svc.run_reminders()
        self.assertEqual(0, r["missed_sign"])
        # 过截止 -> 漏签异常 + 通知
        self.clock.dt = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
        r = self.svc.run_reminders()
        self.assertEqual(1, r["missed_sign"])
        # 幂等：再跑一次不重复通知
        r2 = self.svc.run_reminders()
        self.assertEqual(0, r2["missed_sign"])
        notes = self.conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE kind='missed_sign'"
        ).fetchone()["c"]
        self.assertEqual(1, notes)

    def test_deferred_due_notification(self):
        self.v1_orders()
        box = self.ready_box()
        item = box["items"][0]
        self.svc.administer(
            "n2", item["id"], "deferred", reason="饭后服",
            until_time="2026-10-02T08:30:00+00:00")
        r = self.svc.run_reminders(datetime(2026, 10, 2, 8, 15, tzinfo=UTC))
        self.assertEqual(0, r["deferred_due"])
        r = self.svc.run_reminders(datetime(2026, 10, 2, 8, 31, tzinfo=UTC))
        self.assertEqual(1, r["deferred_due"])


class TestScannerAdapter(unittest.TestCase):
    def test_parse_and_unknown(self):
        from medbox.scanner import ScannerAdapter
        s = ScannerAdapter()
        self.assertEqual("阿司匹林肠溶片",
                         s.read("MED:阿司匹林肠溶片;100mg;B2026")["med_name"])
        self.assertIsNone(s.read("UNKNOWN:xyz"))
        self.assertIsNone(s.read(""))
        self.assertEqual("MED:X;1mg;B2026", s.encode("X", "1mg"))


if __name__ == "__main__":
    unittest.main()
