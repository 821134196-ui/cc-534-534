"""medbox 业务规则测试（标准库 unittest，零依赖）。

覆盖验收要求：
- 旧医嘱版本失效：旧盒作废、不能封盒/签发/交接/执行，同日可按新版重建；
- 双签：配药人与复核人必须不同，缺配药签不能复核，角色限制；
- 重复执行：两人同时确认同一服药格只有一次有效签收（含多线程并发）；
- 拒服闭环：拒服必录原因 → 异常 OPEN → 复核闭环 → 补服签收；
另覆盖：碎裂/错放/数量不符阻断、外出双人复核、漏签、禁忌、护理员无权改医嘱、
哈希链篡改可检出。
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from medbox.db import connect, init_db
from medbox.scanner import MockScanner
from medbox.seed import seed
from medbox.service import MedService, RuleViolation

DATE = "2026-10-02"
ITEMS_V1 = [
    {"med_name": "华法林钠片", "spec": "2.5mg", "dose_qty": 1,
     "meals": ["早餐", "晚餐"]},
    {"med_name": "二甲双胍片", "spec": "0.5g", "dose_qty": 1,
     "meals": ["早餐", "午餐", "晚餐"]},
]
ITEMS_V2 = [
    {"med_name": "二甲双胍片", "spec": "0.5g", "dose_qty": 2,
     "meals": ["早餐", "晚餐"]},
    {"med_name": "阿托伐他汀钙片", "spec": "20mg", "dose_qty": 1,
     "meals": ["睡前"]},
]


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = connect(os.path.join(self.dir, "t.db"))
        init_db(self.conn)
        self.scanner = MockScanner()
        self.svc = MedService(self.conn, self.scanner,
                              photo_dir=os.path.join(self.dir, "photos"))
        s = self.svc
        self.doc = s.add_staff("D1", "李医生", "DOCTOR")
        self.pa = s.add_staff("P1", "王药师", "PHARMACIST")
        self.pb = s.add_staff("P2", "赵药师", "PHARMACIST")
        self.n1 = s.add_staff("N1", "陈护", "NURSE")
        self.n2 = s.add_staff("N2", "刘护", "NURSE")
        self.r = s.add_resident("R1", "张建国", "A-101", "101室")

    def tearDown(self):
        self.conn.close()

    # -- 辅助 --
    def order_v1(self):
        return self.svc.create_order(self.r, self.doc, ITEMS_V1)

    def issued_box(self, date=DATE):
        self.order_v1()
        b = self.svc.create_box(self.r, date)
        for c in self.svc.box_detail(b)["cells"]:
            self.svc.scan_cell(c["id"])
        self.svc.pack(b, self.pa)
        self.svc.issue(b, self.pb)
        return b

    def first_cell(self, box, med="华法林钠片"):
        return next(c for c in self.svc.box_detail(box)["cells"]
                    if c["med_name"] == med)


class TestOrderVersion(Case):
    def test_old_version_box_voided_on_new_delivery(self):
        o1 = self.order_v1()
        old = self.svc.create_box(self.r, DATE)
        for c in self.svc.box_detail(old)["cells"]:
            self.svc.scan_cell(c["id"])
        self.svc.pack(old, self.pa)
        self.svc.issue(old, self.pb)

        o2 = self.svc.create_order(self.r, self.doc, ITEMS_V2,
                                   note="华法林临时停用")

        self.assertEqual(
            self.conn.execute("SELECT status FROM orders WHERE id=?",
                              (o1,)).fetchone()["status"], "SUPERSEDED")
        row = self.conn.execute("SELECT * FROM boxes WHERE id=?",
                                (old,)).fetchone()
        self.assertEqual(row["status"], "VOID")
        self.assertIn("旧版", row["void_reason"])

    def test_void_box_blocks_all_operations(self):
        old = self.issued_box()
        self.svc.create_order(self.r, self.doc, ITEMS_V2)
        cell = self.first_cell(old)
        for fn in [
            lambda: self.svc.pack(old, self.pa),
            lambda: self.svc.issue(old, self.pb),
            lambda: self.svc.execute(cell["id"], "TAKEN", self.n1),
        ]:
            with self.assertRaisesRegex(RuleViolation, "作废|失效"):
                fn()
        # 已作废盒不能交接
        with self.assertRaises(RuleViolation):
            self.svc.handover(old, self.pb, self.n1)

    def test_rebuild_same_day_with_new_version(self):
        old = self.issued_box()
        self.svc.create_order(self.r, self.doc, ITEMS_V2)
        new = self.svc.create_box(self.r, DATE)  # 同日按 v2 重建
        self.assertEqual(
            self.svc.box_detail(new)["order"]["version"], 2)
        meds = {c["med_name"] for c in self.svc.box_detail(new)["cells"]}
        self.assertNotIn("华法林钠片", meds)  # 停药样例：新药盒不含华法林

    def test_nurse_cannot_change_order(self):
        self.order_v1()
        item = self.conn.execute(
            "SELECT id FROM order_items LIMIT 1").fetchone()["id"]
        with self.assertRaisesRegex(RuleViolation, "角色"):
            self.svc.suspend_item(item, self.n1)
        with self.assertRaisesRegex(RuleViolation, "角色"):
            self.svc.create_order(self.r, self.n1, ITEMS_V2)

    def test_item_suspension_blocks_cells_and_issuing(self):
        b = self.issued_box()
        item = self.conn.execute(
            "SELECT id FROM order_items WHERE med_name='二甲双胍片' LIMIT 1"
        ).fetchone()["id"]
        self.svc.suspend_item(item, self.doc)
        blocked = self.conn.execute(
            "SELECT COUNT(*) c FROM box_cells WHERE box_id=? AND status='BLOCKED'",
            (b,)).fetchone()["c"]
        self.assertEqual(blocked, 3)
        # 阻断格不能执行
        c = self.conn.execute(
            "SELECT id FROM box_cells WHERE box_id=? AND status='BLOCKED' LIMIT 1",
            (b,)).fetchone()["id"]
        with self.assertRaisesRegex(RuleViolation, "阻断"):
            self.svc.execute(c, "TAKEN", self.n1)


class TestDualSign(Case):
    def test_preparer_and_checker_must_differ(self):
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        for c in self.svc.box_detail(b)["cells"]:
            self.svc.scan_cell(c["id"])
        self.svc.pack(b, self.pa)
        with self.assertRaisesRegex(RuleViolation, "配药人与复核人不能同"):
            self.svc.issue(b, self.pa)
        self.svc.issue(b, self.pb)  # 换人即可

    def test_issue_requires_pack_first(self):
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        with self.assertRaisesRegex(RuleViolation, "封盒"):
            self.svc.issue(b, self.pb)

    def test_only_pharmacist_roles(self):
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        with self.assertRaisesRegex(RuleViolation, "PHARMACIST"):
            self.svc.pack(b, self.n1)
        for c in self.svc.box_detail(b)["cells"]:
            self.svc.scan_cell(c["id"])
        self.svc.pack(b, self.pa)
        with self.assertRaisesRegex(RuleViolation, "PHARMACIST"):
            self.svc.issue(b, self.n1)

    def test_pack_blocked_until_all_cells_scan_ok(self):
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        with self.assertRaisesRegex(RuleViolation, "未扫码通过"):
            self.svc.pack(b, self.pa)

    def test_contraindication_blocks_issue(self):
        self.svc.add_contraindication(self.r, "华法林钠片", "出血风险禁忌")
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        for c in self.svc.box_detail(b)["cells"]:
            self.svc.scan_cell(c["id"])
        self.svc.pack(b, self.pa)
        with self.assertRaisesRegex(RuleViolation, "禁忌"):
            self.svc.issue(b, self.pb)


class TestDuplicateExecution(Case):
    def test_second_taken_blocked(self):
        b = self.issued_box()
        self.svc.handover(b, self.pb, self.n1)
        c = self.first_cell(b)
        self.svc.execute(c["id"], "TAKEN", self.n1)
        with self.assertRaisesRegex(RuleViolation, "重复给药"):
            self.svc.execute(c["id"], "TAKEN", self.n2)
        taken = self.conn.execute(
            "SELECT COUNT(*) c FROM dose_executions WHERE cell_id=? AND outcome='TAKEN'",
            (c["id"],)).fetchone()["c"]
        self.assertEqual(taken, 1)

    def test_concurrent_double_confirm_one_record(self):
        """两人（两线程/两个客户端连接）同时确认同一服药格：只有一次有效执行。"""
        b = self.issued_box()
        c = self.first_cell(b)
        conn2 = connect(os.path.join(self.dir, "t.db"))
        svc2 = MedService(conn2, self.scanner,
                          photo_dir=os.path.join(self.dir, "photos"))
        barrier = threading.Barrier(2)
        results = []

        def confirm(svc, nurse):
            try:
                barrier.wait()
                svc.execute(c["id"], "TAKEN", nurse)
                results.append("ok")
            except RuleViolation:
                results.append("blocked")

        t1 = threading.Thread(target=confirm, args=(self.svc, self.n1))
        t2 = threading.Thread(target=confirm, args=(svc2, self.n2))
        t1.start(); t2.start(); t1.join(); t2.join()
        conn2.close()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("blocked"), 1)
        taken = self.conn.execute(
            "SELECT COUNT(*) c FROM dose_executions WHERE cell_id=? AND outcome='TAKEN'",
            (c["id"],)).fetchone()["c"]
        self.assertEqual(taken, 1)

    def test_execute_requires_issued_box(self):
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        c = self.first_cell(b)
        with self.assertRaisesRegex(RuleViolation, "未签发"):
            self.svc.execute(c["id"], "TAKEN", self.n1)

    def test_only_nurse_executes(self):
        b = self.issued_box()
        c = self.first_cell(b)
        with self.assertRaisesRegex(RuleViolation, "NURSE"):
            self.svc.execute(c["id"], "TAKEN", self.pa)


class TestRefuseLoop(Case):
    def test_refuse_requires_reason_and_opens_exception(self):
        b = self.issued_box()
        self.svc.handover(b, self.pb, self.n1)
        c = self.first_cell(b)
        with self.assertRaisesRegex(RuleViolation, "原因"):
            self.svc.execute(c["id"], "REFUSED", self.n1, reason="")
        self.svc.execute(c["id"], "REFUSED", self.n1, reason="住户恶心拒服")
        self.assertEqual(
            self.conn.execute("SELECT status FROM box_cells WHERE id=?",
                              (c["id"],)).fetchone()["status"], "REFUSED")
        exc = self.conn.execute(
            "SELECT * FROM exceptions WHERE ref_type='CELL' AND ref_id=? AND status='OPEN'",
            (c["id"],)).fetchone()
        self.assertIsNotNone(exc)
        self.assertIn("恶心拒服", exc["reason"])

    def test_refuse_resolve_then_retake_closes_loop(self):
        b = self.issued_box()
        self.svc.handover(b, self.pb, self.n1)
        c = self.first_cell(b)
        self.svc.execute(c["id"], "REFUSED", self.n1, reason="睡熟叫不醒")
        exc = self.conn.execute(
            "SELECT id FROM exceptions WHERE ref_type='CELL' AND ref_id=? AND status='OPEN'",
            (c["id"],)).fetchone()["id"]
        # 闭环必须填处理结果
        with self.assertRaises(RuleViolation):
            self.svc.resolve_exception(exc, self.n2, "  ")
        self.svc.resolve_exception(exc, self.n2, "已报告值班医师，1小时后叫醒补服")
        # 补服签收：唯一有效执行；异常自动闭环
        self.svc.execute(c["id"], "TAKEN", self.n2)
        self.assertEqual(
            self.conn.execute("SELECT status FROM exceptions WHERE id=?",
                              (exc,)).fetchone()["status"], "RESOLVED")
        self.assertEqual(
            self.conn.execute("SELECT status FROM box_cells WHERE id=?",
                              (c["id"],)).fetchone()["status"], "TAKEN")

    def test_deferred_resident_out_needs_witness(self):
        b = self.issued_box()
        c = self.first_cell(b)
        self.svc.set_resident_out(self.r, True)
        with self.assertRaisesRegex(RuleViolation, "外出"):
            self.svc.execute(c["id"], "TAKEN", self.n1)
        with self.assertRaisesRegex(RuleViolation, "第二人"):
            self.svc.execute(c["id"], "DEFERRED", self.n1, reason="外出未归")
        # 同一人不能充当第二确认人
        with self.assertRaisesRegex(RuleViolation, "同一人"):
            self.svc.execute(c["id"], "DEFERRED", self.n1,
                             reason="外出未归", witness_id=self.n1)
        self.svc.execute(c["id"], "DEFERRED", self.n1,
                         reason="随家属外出，晚间返回补服", witness_id=self.n2)


class TestScanFaults(Case):
    def _fresh_box(self):
        self.order_v1()
        b = self.svc.create_box(self.r, DATE)
        return b, self.first_cell(b)

    def test_crushed_tablet_blocks(self):
        _, c = self._fresh_box()
        self.scanner.inject(c["id"], crushed=True)
        row = self.svc.scan_cell(c["id"])
        self.assertEqual(row["scan_flag"], "CRUSHED")
        self.assertEqual(row["status"], "BLOCKED")

    def test_wrong_cell_blocks(self):
        _, c = self._fresh_box()
        self.scanner.inject(c["id"], slot="晚餐")
        row = self.svc.scan_cell(c["id"])
        self.assertEqual(row["scan_flag"], "WRONG_CELL")

    def test_wrong_med_spec_qty_block(self):
        _, c = self._fresh_box()
        self.scanner.inject(c["id"], med_name="其他药片")
        self.assertEqual(self.svc.scan_cell(c["id"])["scan_flag"], "WRONG_MED")
        self.svc.resolve_exception(
            self.conn.execute(
                "SELECT id FROM exceptions WHERE ref_id=? AND status='OPEN'",
                (c["id"],)).fetchone()["id"],
            self.pa, "清理错药")
        self.svc.repack_cell(c["id"], self.pa)
        self.scanner.inject(c["id"], spec="10mg")
        self.assertEqual(self.svc.scan_cell(c["id"])["scan_flag"], "SPEC_MISMATCH")
        self.svc.resolve_exception(
            self.conn.execute(
                "SELECT id FROM exceptions WHERE ref_id=? AND status='OPEN'",
                (c["id"],)).fetchone()["id"],
            self.pa, "更换规格")
        self.svc.repack_cell(c["id"], self.pa)
        self.scanner.inject(c["id"], qty=3)
        self.assertEqual(self.svc.scan_cell(c["id"])["scan_flag"], "QTY_MISMATCH")

    def test_repack_stopped_med_rejected(self):
        b = self.issued_box()
        item = self.conn.execute(
            "SELECT id FROM order_items WHERE med_name='华法林钠片' LIMIT 1"
        ).fetchone()["id"]
        self.svc.suspend_item(item, self.doc)
        c = self.conn.execute(
            "SELECT id FROM box_cells WHERE order_item_id=? AND status='BLOCKED'",
            (item,)).fetchone()
        with self.assertRaisesRegex(RuleViolation, "停用"):
            self.svc.repack_cell(c["id"], self.pa)

    def test_missed_scan(self):
        b = self.issued_box()
        c = self.first_cell(b)
        # 早餐 08:00 + 30 分钟后扫描漏签
        at = datetime.strptime(f"{DATE} 09:00", "%Y-%m-%d %H:%M")
        n = self.svc.scan_missed(at)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM box_cells WHERE id=?",
                              (c["id"],)).fetchone()["status"], "MISSED")


class TestChainAndSeed(unittest.TestCase):
    def test_chain_detects_tampering(self):
        conn = connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        ids = seed(conn, DATE)
        self.assertEqual(MedService.verify_chain(conn), [])
        conn.execute("UPDATE chain SET payload='{\"tampered\":true}' WHERE seq=1")
        conn.commit()
        self.assertIn(1, MedService.verify_chain(conn))
        conn.close()

    def test_seed_contains_stop_sample_and_open_exception(self):
        conn = connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        ids = seed(conn, DATE)
        old = conn.execute("SELECT * FROM boxes WHERE id=?",
                           (ids["boxes"]["old_r1_v1"],)).fetchone()
        self.assertEqual(old["status"], "VOID")
        self.assertEqual(
            conn.execute("SELECT version_snapshot FROM boxes WHERE id=?",
                         (ids["boxes"]["today_r1"],)).fetchone()[0], 2)
        open_n = conn.execute(
            'SELECT COUNT(*) c FROM exceptions WHERE status="OPEN"').fetchone()["c"]
        self.assertGreaterEqual(open_n, 1)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
