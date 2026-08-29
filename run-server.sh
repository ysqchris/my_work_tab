#!/bin/bash
# LaunchAgent / 守护进程入口：保证在干净环境下用 venv 启动
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
export PORT="${PORT:-8765}"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"

# 工作台数据目录：默认用持久目录，避免 fallback 到代码目录的 data/ 造成"数据丢失"
export WORKTAB_DATA_DIR="${WORKTAB_DATA_DIR:-/root/.openclaw/workspace/data/my_work_tab}"

# 从 .env 加载混元等其它配置（KEY=VALUE 形式，值不含单引号，安全 source）
if [[ -f "$DIR/.env" ]]; then
  set -a
  . "$DIR/.env"
  set +a
fi

# 从 .env.bili 加载 B 站登录 Cookie（存在则导入，不存在/被忽略则静默跳过）
# 注意：BILI_COOKIE 值可能含单引号等特殊字符，不能用 source，只能用逐行 export
if [[ -f "$DIR/.env.bili" ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    k="$(printf '%s' "$k" | tr -d '\r\n')"
    v="${v%$'\r'}"
    export "$k"="$v"
  done < "$DIR/.env.bili"
fi

exec "$DIR/.venv/bin/python3" "$DIR/app.py"
