#!/bin/bash
# 工作台服务管理脚本
# 用法:
#   ./manage.sh start | stop | restart | status
#   ./manage.sh install   # 注册 macOS 登录自启（推荐，崩溃自动拉起）
#   ./manage.sh uninstall # 取消自启
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
VENV_PY="$DIR/.venv/bin/python3"
PORT=8765
LOG="$DIR/server.log"
LAUNCH_LOG="$HOME/Library/Logs/chris-workbench.log"
PID_FILE="$DIR/server.pid"
LABEL="com.chris.workbench"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

alive() {
  curl -s "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1
}

# 双 fork 脱离 Cursor/终端进程组，避免会话结束被一并杀掉
daemon_start() {
  "$VENV_PY" - "$DIR" "$LOG" "$PID_FILE" <<'PY'
import os, sys, time
base, log, pid_file = sys.argv[1:4]
runner = os.path.join(base, "run-server.sh")
if os.fork():
    time.sleep(0.3)
    sys.exit(0)
os.setsid()
if os.fork():
    sys.exit(0)
os.chdir(base)
os.environ["PORT"] = os.environ.get("PORT", "8765")
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1)
os.dup2(fd, 2)
os.close(fd)
devnull = os.open(os.devnull, os.O_RDONLY)
os.dup2(devnull, 0)
os.close(devnull)
with open(pid_file, "w") as f:
    f.write(str(os.getpid()))
os.execv("/bin/bash", ["/bin/bash", runner])
PY
}

pid_of() {
  if [[ -f "$PID_FILE" ]]; then
    local p
    p=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then
      echo "$p"
      return 0
    fi
  fi
  # 兜底：按端口找
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true
}

start() {
  if alive; then
    echo "工作台已在运行: http://localhost:$PORT"
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    echo "❌ 找不到虚拟环境: $VENV_PY"
    exit 1
  fi
  echo "启动 工作台…"
  : > "$LOG"
  daemon_start
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if alive; then
      echo "✅ 启动成功: http://localhost:$PORT  (pid $(pid_of))"
      return 0
    fi
    sleep 0.4
  done
  echo "❌ 启动失败，查看 $LOG"
  tail -30 "$LOG" || true
  exit 1
}

stop() {
  local p
  p=$(pid_of)
  # 若装了 LaunchAgent，先停掉，避免 KeepAlive 立刻拉起
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  fi
  if [[ -n "$p" ]]; then
    kill "$p" 2>/dev/null || true
    sleep 0.5
    kill -9 "$p" 2>/dev/null || true
    echo "已停止 (pid $p)"
  else
    pkill -f "$DIR/app.py" 2>/dev/null && echo "已停止" || echo "没有在运行的进程"
  fi
  rm -f "$PID_FILE"
}

status() {
  if alive; then
    echo "✅ 运行中: http://localhost:$PORT  (pid $(pid_of))"
    if [[ -f "$PLIST" ]]; then
      echo "   登录自启: 已安装 ($LABEL)"
    fi
  else
    echo "⏹ 未运行"
    if [[ -f "$PLIST" ]]; then
      echo "   登录自启: 已安装，可执行 ./manage.sh start 或重登"
    fi
  fi
}

install() {
  mkdir -p "$(dirname "$PLIST")"
  chmod +x "$DIR/run-server.sh"
  mkdir -p "$(dirname "$LAUNCH_LOG")"
  # 日志放 Library，避免 LaunchAgent 写 Documents 被 TCC 拦截
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/run-server.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>$LAUNCH_LOG</string>
  <key>StandardErrorPath</key>
  <string>$LAUNCH_LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORT</key>
    <string>$PORT</string>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
EOF
  local p
  p=$(pid_of)
  if [[ -n "$p" ]]; then
    kill "$p" 2>/dev/null || true
    sleep 0.3
  fi
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
  sleep 1.5
  if alive; then
    echo "✅ 已安装登录自启，并已启动: http://localhost:$PORT"
    echo "   开机/崩溃会自动拉起。日志: $LAUNCH_LOG"
  else
    echo "⚠️  LaunchAgent 可能受 macOS 对 Documents 目录的后台访问限制。"
    echo "   已改用守护进程方式启动（不依赖 LaunchAgent）…"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    start
    echo ""
    echo "更省事的用法：在系统「终端」里执行一次："
    echo "  cd \"$DIR\" && ./manage.sh start"
    echo "服务会挂到系统进程下，关 Cursor / 强刷页面都不会掉。"
    echo "若要开机自启，把项目挪出 Documents，或给 /bin/bash 开「完全磁盘访问」后再 ./manage.sh install"
  fi
}

uninstall() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已取消登录自启。正在停止当前服务…"
  stop
}

case "$1" in
  start) start;;
  stop) stop;;
  restart) stop; sleep 1; start;;
  status) status;;
  install) install;;
  uninstall) uninstall;;
  *) echo "用法: $0 {start|stop|restart|status|install|uninstall}"; exit 1;;
esac
