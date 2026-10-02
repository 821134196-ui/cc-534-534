#!/usr/bin/env bash
# 单条本地命令启动分药盒管理系统（自动生成停药样例）
set -e
cd "$(dirname "$0")"
exec python3 -m medbox "$@"
