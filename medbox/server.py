"""HTTP 服务（标准库 http.server）：JSON API + 静态页面。

启动：python3 -m medbox [--db data/medbox.db] [--port 8000]
单条本地命令：自动建库、播种停药样例、启动进程内提醒、打开 Web 界面。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .db import connect, init_db
from .reminders import ReminderScheduler
from .scanner import MockScanner
from .seed import seed
from .service import MedService, RuleViolation, SLOTS

WEB_DIR = Path(__file__).parent / "web"
PHOTO_PREFIX = "/photos/"


class AppState:
    def __init__(self, db_path: str, photo_dir: str):
        self.conn = connect(db_path)
        init_db(self.conn)
        self.scanner = MockScanner()
        self.svc = MedService(self.conn, scanner=self.scanner, photo_dir=photo_dir)
        self.lock = threading.RLock()  # 串行化写操作
        self.photo_dir = Path(photo_dir)
        self.ids = seed(self.conn, scanner=self.scanner, photo_dir=photo_dir)


def build_api(state: AppState) -> dict:
    return {
        "state": lambda q, b: state_summary(state),
        "box": lambda q, b: state.svc.box_detail(int(q["id"][0])),
        "pending": lambda q, b: state.svc.pending_cells(q.get("bed", [None])[0]),
        "exceptions": lambda q, b: {
            "open": [dict(r) for r in state.svc.open_exceptions()],
            "all": [dict(r) for r in state.svc.conn.execute(
                "SELECT e.*, r.name resident_name, r.bed_no FROM exceptions e"
                " LEFT JOIN residents r ON r.id=e.resident_id"
                " ORDER BY e.id DESC LIMIT 100")],
        },
        "reminders": lambda q, b: [dict(r) for r in state.svc.conn.execute(
            "SELECT * FROM reminders ORDER BY id DESC LIMIT 50")],
        "chain": lambda q, b: {
            "events": [dict(r) for r in state.svc.conn.execute(
                "SELECT seq,at,event_type,ref,payload FROM chain ORDER BY seq DESC LIMIT 50")],
            "tampered": MedService.verify_chain(state.svc.conn),
        },
        "scan": lambda q, b: dict(state.svc.scan_cell(int(b["cell_id"]))),
        "inject-fault": lambda q, b: _inject_fault(state, b),
        "repack": lambda q, b: state.svc.repack_cell(
            int(b["cell_id"]), int(b["staff_id"])) and {"ok": True},
        "remove-cell": lambda q, b: (
            state.svc.remove_cell(int(b["cell_id"]), int(b["staff_id"])),
            {"ok": True})[1],
        "pack": lambda q, b: {"photo": state.svc.pack(
            int(b["box_id"]), int(b["staff_id"]))},
        "issue": lambda q, b: (
            state.svc.issue(int(b["box_id"]), int(b["staff_id"])), {"ok": True})[1],
        "handover": lambda q, b: (
            state.svc.handover(int(b["box_id"]),
                               b.get("from_id") and int(b["from_id"]),
                               int(b["to_id"])),
            {"ok": True})[1],
        "execute": lambda q, b: state.svc.execute(
            int(b["cell_id"]), b["outcome"], int(b["staff_id"]),
            b.get("reason", ""),
            b.get("witness_id") and int(b["witness_id"])),
        "resolve-exception": lambda q, b: (
            state.svc.resolve_exception(int(b["exc_id"]), int(b["staff_id"]),
                                        b.get("resolution", "")),
            {"ok": True})[1],
        "suspend": lambda q, b: (
            state.svc.suspend_item(int(b["item_id"]), int(b["staff_id"])),
            {"ok": True})[1],
        "resident-out": lambda q, b: (
            state.svc.set_resident_out(int(b["resident_id"]),
                                      bool(b.get("out", True))),
            {"ok": True})[1],
        "new-order": lambda q, b: {"order_id": _new_order(state, b)},
        "reminder-tick": lambda q, b: _scheduler.tick(),
    }


def _inject_fault(state: AppState, b: dict) -> dict:
    """测试/演示用：为下一次扫码注入异常读数。"""
    cell_id = int(b["cell_id"])
    kind = b.get("kind", "crushed")
    fault = {"crushed": {"crushed": True},
             "wrong_cell": {"slot": b.get("slot", "晚餐")},
             "wrong_med": {"med_name": b.get("med_name", "未知药片X")},
             "spec": {"spec": b.get("spec", "10mg")},
             "qty": {"qty": float(b.get("qty", 9))}}[kind]
    state.scanner.inject(cell_id, **fault)
    return {"ok": True, "next_reading": fault}


def _new_order(state: AppState, b: dict) -> int:
    items = b.get("items")
    if isinstance(items, str):
        items = json.loads(items)
    return state.svc.create_order(
        int(b["resident_id"]), int(b["staff_id"]), items, b.get("note", ""))


def state_summary(state: AppState) -> dict:
    conn = state.svc.conn
    staff = [dict(r) for r in conn.execute("SELECT * FROM staff ORDER BY role,id")]
    residents = [dict(r) for r in conn.execute(
        "SELECT * FROM residents ORDER BY bed_no")]
    boxes = [dict(r) for r in conn.execute(
        "SELECT b.*, r.name resident_name FROM boxes b"
        " JOIN residents r ON r.id=b.resident_id"
        " ORDER BY b.med_date DESC, b.id DESC")]
    for bx in boxes:
        bx["cells"] = [dict(c) for c in conn.execute(
            "SELECT * FROM box_cells WHERE box_id=? ORDER BY position", (bx["id"],))]
        bx["preparer_name"] = _name(conn, bx["preparer_id"])
        bx["checker_name"] = _name(conn, bx["checker_id"])
    orders = [dict(r) for r in conn.execute(
        "SELECT o.*, s.name doctor_name FROM orders o"
        " JOIN staff s ON s.id=o.created_by ORDER BY o.resident_id, o.version")]
    for o in orders:
        o["items"] = [dict(r) for r in conn.execute(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY seq", (o["id"],))]
    return {
        "staff": staff, "residents": residents, "boxes": boxes,
        "orders": orders, "slots": SLOTS,
        "open_exception_count": conn.execute(
            'SELECT COUNT(*) c FROM exceptions WHERE status="OPEN"').fetchone()["c"],
        "roles": {"DOCTOR": "医师", "PHARMACIST": "药师", "NURSE": "护理员"},
        "status_text": {
            "DRAFT": "待装盒", "PACKED": "已封盒待签", "ISSUED": "已签发",
            "VOID": "已作废", "PENDING": "待扫码", "SCANNED_OK": "扫码通过",
            "BLOCKED": "阻断", "TAKEN": "已签收", "REFUSED": "拒服",
            "DEFERRED": "暂缓", "MISSED": "漏签"},
    }


def _name(conn, staff_id):
    if not staff_id:
        return ""
    row = conn.execute("SELECT name FROM staff WHERE id=?", (staff_id,)).fetchone()
    return row["name"] if row else ""


_scheduler: ReminderScheduler | None = None


class Handler(BaseHTTPRequestHandler):
    state: AppState = None  # 由 main 注入到类属性

    def log_message(self, fmt, *args):
        pass  # 安静；需要时可打开

    # ---------------- 响应工具 ----------------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- 路由 ----------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            name = path[5:]
            api = build_api(self.state)
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            if name not in api or name in _POST_ONLY:
                return self._send_json({"error": f"未知接口 {name}"}, 404)
            return self._dispatch(name, q, {}, "GET")
        if path == "/" or path == "":
            return self._send_file(WEB_DIR / "index.html")
        if path.startswith(PHOTO_PREFIX):
            return self._send_file(self.state.photo_dir / path[len(PHOTO_PREFIX):])
        if path.startswith("/static/"):
            return self._send_file(WEB_DIR / path[len("/static/"):])
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return
        name = parsed.path[5:]
        api = build_api(self.state)
        if name not in api or name in _GET_ONLY:
            return self._send_json({"error": f"未知接口 {name}"}, 404)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "请求体不是合法 JSON"}, 400)
        self._dispatch(name, {}, body, "POST")

    def _dispatch(self, name, query, body, method):
        api = build_api(self.state)
        try:
            with self.state.lock:
                result = api[name](query, body)
            self._send_json({"ok": True, "data": result})
        except RuleViolation as exc:
            self._send_json({"ok": False, "error": str(exc)}, 409)
        except (KeyError, ValueError, TypeError) as exc:
            self._send_json({"ok": False, "error": f"参数错误：{exc}"}, 400)


_POST_ONLY = {"scan", "inject-fault", "repack", "remove-cell", "pack", "issue",
              "handover", "execute", "resolve-exception", "suspend",
              "resident-out", "new-order", "reminder-tick"}
_GET_ONLY = {"state", "box", "pending", "exceptions", "reminders", "chain"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="养老机构分药盒管理系统")
    parser.add_argument("--db", default="data/medbox.db")
    parser.add_argument("--photos", default="data/photos")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args(argv)

    state = AppState(args.db, args.photos)
    Handler.state = state

    global _scheduler
    _scheduler = ReminderScheduler(state.svc, interval_sec=60)
    _scheduler.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"分药盒管理系统已启动：http://{args.host}:{args.port}/")
    print(f"  配药/复核端：http://{args.host}:{args.port}/static/pharmacy.html")
    print(f"  床旁执行端：http://{args.host}:{args.port}/static/bedside.html")
    print(f"  数据库：{args.db}（含临时停药样例），提醒任务进程内运行")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _scheduler.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
