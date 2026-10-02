"""本地 HTTP 服务：零依赖（stdlib http.server）。

路由：
  GET  /                 单页应用（药盒台 / 床旁端 / 异常追踪）
  GET  /api/board        当日看板数据
  POST /api/call         统一领域调用 {"op": ..., "args": {...}}
  GET  /photos/<name>    封盒照片
所有写操作经同一把锁串行化，SQLite 连接线程安全。
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .core import DomainError, MedBoxService
from .photos import save_seal_photo
from .reminders import ReminderScheduler

STATIC_DIR = Path(__file__).parent / "static"

# 允许前端调用的服务方法白名单及参数名
OPS = {
    "new_version":        (["note"], {}),
    "add_order":          (["resident_id", "med_name", "spec", "dose_text",
                            "meal_slot", "quantity"], {"quantity": 1.0}),
    "stop_order_temp":    (["order_ids", "reason"], {}),
    "set_away":           (["resident_id", "away", "reason", "expected_return"],
                           {"reason": "", "expected_return": ""}),
    "pack_box":           (["resident_id", "serve_date", "meal_slot"], {}),
    "void_box":           (["box_id", "reason"], {}),
    "flag_item":          (["item_id", "state", "note"], {"note": ""}),
    "resolve_item":       (["item_id", "new_cell_no", "note"], {}),
    "sign_box":           (["box_id"], {}),
    "handoff":            (["box_id", "to_nurse_id", "note"], {"note": ""}),
    "scan":               (["code"], {}),
    "administer":         (["item_id", "result", "reason", "until_time",
                            "scan_code"], {"reason": "", "until_time": None,
                                           "scan_code": None}),
    "followup_deferred":  (["admin_id", "final_result", "reason", "scan_code"],
                           {"reason": "", "scan_code": None}),
    "close_exception":    (["exception_id", "resolution"], {}),
    "run_reminders":      ([], {}),
}


class AppState:
    def __init__(self, conn, photo_dir: str):
        self.conn = conn
        self.photo_dir = photo_dir
        self.svc = MedBoxService(conn)
        self.lock = threading.RLock()
        self.scheduler = ReminderScheduler(self._tick, interval_seconds=20.0)

    def _tick(self) -> dict:
        with self.lock:
            return self.svc.run_reminders()

    def call(self, op: str, actor_id: str, payload: dict) -> Any:
        if op not in OPS:
            raise DomainError("BAD_OP", f"未知操作：{op}")
        listed, defaults = OPS[op]
        required = [k for k in listed if k not in defaults]
        kwargs = dict(defaults)
        for key in required:
            if key not in payload:
                raise DomainError("BAD_PARAM", f"缺少参数：{key}")
            kwargs[key] = payload[key]
        # defaults 中声明的是可选参数；payload 里给了就覆盖
        for key in defaults:
            if key in payload:
                kwargs[key] = payload[key]
        # 所有操作都需要操作者（run_reminders 用系统账户）
        if op == "run_reminders":
            with self.lock:
                return self.svc.run_reminders()

        with self.lock:
            method = getattr(self.svc, op)
            result = method(actor_id, **kwargs)
            # 封盒动作需要写本地照片：由服务端在 sign 之后单独提供
            return self._serialize(result)

    def seal(self, actor_id: str, box_id: int) -> dict:
        with self.lock:
            box = self.svc._box(box_id)
            path = save_seal_photo(self.photo_dir, box_id, actor_id, self.svc.now())
            self.svc.seal_box(actor_id, box_id, path)
            return self._serialize(self.svc.box_detail(box_id))

    def board(self, date: str) -> dict:
        with self.lock:
            return self.svc.board(date)

    @staticmethod
    def _serialize(result: Any) -> Any:
        if isinstance(result, (int, float, str, bool)) or result is None:
            return result
        if isinstance(result, dict):
            return {k: AppState._serialize(v) for k, v in result.items()}
        if isinstance(result, list):
            return [AppState._serialize(v) for v in result]
        return str(result)


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MedBox/1.0"

        def log_message(self, fmt, *args):
            pass  # 安静模式；错误由前端展示

        def _json(self, obj: Any, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str):
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if parsed.path == "/app.js":
                return self._file(STATIC_DIR / "app.js",
                                  "application/javascript; charset=utf-8")
            if parsed.path == "/api/board":
                qs = urllib.parse.parse_qs(parsed.query)
                date = qs.get("date", [""])[0] or state.svc.now().date().isoformat()
                try:
                    return self._json({"ok": True, "data": state.board(date)})
                except DomainError as e:
                    return self._json({"ok": False, "error": {"code": e.code,
                                                              "message": e.message}}, 409)
            if parsed.path.startswith("/photos/"):
                name = Path(urllib.parse.unquote(parsed.path[len("/photos/"):])).name
                return self._file(Path(state.photo_dir) / name, "image/png")
            if parsed.path == "/favicon.ico":
                self.send_response(204); self.end_headers(); return
            self.send_error(404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/api/call", "/api/seal"):
                return self.send_error(404)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": {"code": "BAD_JSON",
                                                          "message": "请求不是合法 JSON"}}, 400)
            actor_id = self.headers.get("X-Actor") or payload.pop("actor_id", None) or ""
            if not actor_id:
                return self._json({"ok": False, "error": {"code": "NO_ACTOR",
                                                          "message": "未选择操作人"}}, 401)
            try:
                if parsed.path == "/api/seal":
                    data = state.seal(actor_id, int(payload["box_id"]))
                else:
                    data = state.call(payload.get("op", ""), actor_id,
                                      payload.get("args", {}))
                return self._json({"ok": True, "data": data})
            except DomainError as e:
                level = "high" if e.code in (
                    "STALE_BOX", "CONTRAINDICATION", "BROKEN_PILL",
                    "MISPLACED_CELL", "RESIDENT_AWAY",
                    "SCAN_MISMATCH", "CONTENT_MISMATCH", "SAME_PERSON") else "warn"
                return self._json({"ok": False, "error": {"code": e.code,
                                                          "message": e.message,
                                                          "level": level}}, 409)
            except (KeyError, TypeError, ValueError) as e:
                return self._json({"ok": False, "error": {"code": "BAD_PARAM",
                                                          "message": str(e)}}, 400)

    return Handler


def serve(conn, photo_dir: str, host: str = "127.0.0.1", port: int = 8080,
          with_reminders: bool = True) -> ThreadingHTTPServer:
    state = AppState(conn, photo_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    if with_reminders:
        state.scheduler.start()
    httpd.medbox_state = state
    return httpd
