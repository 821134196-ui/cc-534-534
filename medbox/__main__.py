"""命令行入口：python3 -m medbox [--db data/medbox.db] [--port 8080] [--reset]

单条本地命令完成：
  1. 初始化 SQLite（签发链所有表）
  2. 首次启动自动播种"临时停药"等全套样例数据
  3. 启动本地 HTTP 服务 + 进程内提醒任务（后台线程，每 20 秒）
"""

from __future__ import annotations

import argparse
import sys
import time

from .core import MedBoxService
from .db import connect, init_db
from .seed import ensure_seeded
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="medbox",
        description="养老机构分药盒管理系统：医嘱版本 / 双人复核 / 床旁执行 / 异常追踪",
    )
    parser.add_argument("--db", default="data/medbox.db", help="SQLite 文件路径")
    parser.add_argument("--photos", default="data/photos", help="封盒照片目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reset", action="store_true",
                        help="清空数据库并重新生成停药样例")
    parser.add_argument("--no-reminders", action="store_true",
                        help="不启动进程内提醒线程（测试用）")
    parser.add_argument("--seed-only", action="store_true",
                        help="只建库播种，不起服务（CI / 冒烟用）")
    args = parser.parse_args(argv)

    print("=" * 64)
    print("  养老机构分药盒管理系统")
    print("=" * 64)

    info = ensure_seeded(args.db, args.photos, force=args.reset)
    if info.get("skipped"):
        print(f"  数据库已存在：{args.db}（保留现有数据，加 --reset 可重建样例）")
    else:
        print(f"  已初始化并生成停药样例：")
        print(f"    当前医嘱版本      v{info['current_version']}")
        print(f"    临时停药旧盒      #{info['stale_box_id']}（已阻断后作废）")
        print(f"    版本更新后新盒    #{info['new_box_id']}（v2 等待床旁执行）")
        print(f"    床旁已完成盒      #{info['r1_breakfast_box']}")
        print(f"    晚餐暂缓格位所在盒 #{info['r1_dinner_deferred_box']}")
        print(f"    昨日漏签药盒      #{info['r4_missed_box']}")
        print(f"    拒服闭环样例盒    #{info['r5_refused_box']}")

    if args.seed_only:
        # 启动时先跑一次提醒，生成漏签通知
        conn = connect(args.db)
        result = MedBoxService(conn).run_reminders()
        conn.close()
        print(f"  提醒任务：暂缓到期 {result['deferred_due']}，漏签 {result['missed_sign']}")
        print("  样例生成完毕（--seed-only）。")
        return 0

    conn = connect(args.db)
    init_db(conn)
    # 启动时先跑一次提醒：把昨日漏签 / 已到期暂缓立刻通知出来
    first = MedBoxService(conn).run_reminders()
    print(f"  启动提醒：暂缓到期 {first['deferred_due']}，漏签 {first['missed_sign']}")
    httpd = serve(conn, args.photos, host=args.host, port=args.port,
                  with_reminders=not args.no_reminders)
    print("-" * 64)
    print(f"  服务已启动：http://{args.host}:{args.port}/")
    print("  操作人切换在页面顶部；视图：药盒台 / 床旁端 / 医嘱住户 / 异常追踪 / 通知流水")
    print("  按 Ctrl+C 停止。")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在停止…")
        httpd.medbox_state.scheduler.stop()
        httpd.shutdown()
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
