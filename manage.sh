#!/bin/bash
# AI情报站服务管理脚本
# 用法: ./manage.sh start | stop | restart | status
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
VENV="$DIR/.venv/bin/activate"
PORT=8765
LOG="$DIR/server.log"

start() {
  if curl -s "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo "AI情报站已在运行 (port $PORT)"
    return 0
  fi
  echo "启动 AI情报站…"
  source "$VENV"
  nohup python3 app.py > "$LOG" 2>&1 &
  sleep 2
  if curl -s "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo "✅ 启动成功: http://localhost:$PORT"
  else
    echo "❌ 启动失败，查看 $LOG"
    tail -20 "$LOG"
  fi
}

stop() {
  pkill -f "app.py" && echo "已停止" || echo "没有在运行的进程"
}

status() {
  if curl -s "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo "✅ 运行中: http://localhost:$PORT"
  else
    echo "⏹ 未运行"
  fi
}

case "$1" in
  start) start;;
  stop) stop;;
  restart) stop; sleep 1; start;;
  status) status;;
  *) echo "用法: $0 {start|stop|restart|status}"; exit 1;;
esac
