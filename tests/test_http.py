"""HTTP 端到端集成测试：真实启动本地服务，验证页面、角色强制、阻断 JSON、照片可读。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from medbox.core import MedBoxService
from medbox.db import connect, init_db
from medbox.server import serve


def _post(base, path, payload, actor):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Actor": actor}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "h.db")
        cls.photo_dir = str(Path(cls.tmp.name) / "photos")
        conn = connect(cls.db_path)
        init_db(conn)
        svc = MedBoxService(conn, clock=lambda: datetime(2026, 10, 2, 8, tzinfo=timezone.utc))
        conn.execute("INSERT INTO users VALUES('doc','张医生','physician')")
        conn.execute("INSERT INTO users VALUES('pck','陈配药','packer')")
        conn.execute("INSERT INTO users VALUES('phm','李药师','pharmacist')")
        conn.execute("INSERT INTO users VALUES('n1','王护','nurse')")
        conn.execute("INSERT INTO users VALUES('n2','赵护','nurse')")
        conn.execute("INSERT INTO residents(id,name,bed_no,allergies,away,away_reason,expected_return)"
                     " VALUES('r1','王秀兰','A-101','',0,NULL,NULL)")
        conn.commit()
        svc.new_version("doc", "v1")
        cls.order_id = svc.add_order("doc", "r1", "阿司匹林肠溶片", "100mg",
                                     "1片 口服", "lunch", 1)
        conn.commit()
        conn.close()

        conn = connect(cls.db_path)
        cls.httpd = serve(conn, cls.photo_dir, host="127.0.0.1", port=0,
                          with_reminders=False)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, op, args, actor):
        return _post(self.base, "/api/call", {"op": op, "args": args}, actor)

    def test_index_and_static_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("分药盒", html)
        with urllib.request.urlopen(self.base + "/app.js", timeout=10) as r:
            # app.js 在 / 根路径之外？实际挂在同目录，前端引用相对路径 app.js
            self.assertEqual(200, r.status)

    def test_full_chain_and_blocks_over_http(self):
        # 护理员装盒 -> 403 业务错误
        st, body = self.call("pack_box",
                             {"resident_id": "r1", "serve_date": "2026-10-02",
                              "meal_slot": "lunch"}, "n1")
        self.assertEqual(409, st)
        self.assertFalse(body["ok"])
        self.assertEqual("FORBIDDEN", body["error"]["code"])

        # 配药员装盒
        st, body = self.call("pack_box",
                             {"resident_id": "r1", "serve_date": "2026-10-02",
                              "meal_slot": "lunch"}, "pck")
        self.assertEqual(200, st, body)
        box_id = body["data"]["id"]

        # 本人（装盒者若是 packer 角色不能签发）——药师签发成功（不同人）
        st, body = self.call("sign_box", {"box_id": box_id}, "phm")
        self.assertEqual(200, st, body)

        # 封盒（服务端生成本地照片）
        st, body = _post(self.base, "/api/seal", {"box_id": box_id}, "phm")
        self.assertEqual(200, st, body)
        photo = body["data"]["sealed_photo"]
        self.assertTrue(Path(photo).is_file())
        # 照片可经 HTTP 读取
        name = Path(photo).name
        with urllib.request.urlopen(self.base + "/photos/" + name, timeout=10) as r:
            png = r.read()
        self.assertTrue(png.startswith(b"\x89PNG"))

        # 交接
        st, body = self.call("handoff", {"box_id": box_id, "to_nurse_id": "n2",
                                         "note": "交接"}, "n1")
        self.assertEqual(200, st, body)

        # 看板含该盒
        with urllib.request.urlopen(self.base + "/api/board?date=2026-10-02",
                                    timeout=10) as r:
            board = json.loads(r.read().decode("utf-8"))
        self.assertTrue(board["ok"])
        self.assertEqual(1, len(board["data"]["boxes"]))

        # 床旁签收（扫码一致）
        item_id = board["data"]["boxes"][0]["items"][0]["id"]
        st, body = self.call("administer",
                             {"item_id": item_id, "result": "taken",
                              "scan_code": "MED:阿司匹林肠溶片;100mg;B2026"}, "n2")
        self.assertEqual(200, st, body)

        # 重复签收 -> 409 + DUPLICATE_ADMIN
        st, body = self.call("administer",
                             {"item_id": item_id, "result": "taken",
                              "scan_code": "MED:阿司匹林肠溶片;100mg;B2026"}, "n1")
        self.assertEqual(409, st)
        self.assertEqual("DUPLICATE_ADMIN", body["error"]["code"])
        self.assertEqual("warn", body["error"]["level"])

    def test_stale_version_over_http(self):
        # 装第二个盒，然后医生停药升版本，旧盒签发被阻断
        st, body = self.call("pack_box",
                             {"resident_id": "r1", "serve_date": "2026-10-03",
                              "meal_slot": "lunch"}, "pck")
        self.assertEqual(200, st, body)
        box_id = body["data"]["id"]

        st, body = self.call("stop_order_temp",
                             {"order_ids": [self.order_id], "reason": "出血"}, "doc")
        self.assertEqual(200, st, body)
        self.assertEqual(2, body["data"])

        st, body = self.call("sign_box", {"box_id": box_id}, "phm")
        self.assertEqual(409, st)
        self.assertEqual("STALE_BOX", body["error"]["code"])
        self.assertEqual("high", body["error"]["level"])

    def test_requires_actor(self):
        req = urllib.request.Request(
            self.base + "/api/call",
            data=json.dumps({"op": "run_reminders", "args": {}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应拒绝无操作人请求")
        except urllib.error.HTTPError as e:
            self.assertEqual(401, e.code)


if __name__ == "__main__":
    unittest.main()
