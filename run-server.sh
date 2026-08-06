#!/bin/bash
# LaunchAgent / 守护进程入口：保证在干净环境下用 venv 启动
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
export PORT="${PORT:-8765}"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"
exec "$DIR/.venv/bin/python3" "$DIR/app.py"
