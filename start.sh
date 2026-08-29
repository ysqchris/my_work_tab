#!/bin/bash
# 启动工作台服务：用 Python 读取 .env.bili 注入 BILI_COOKIE，避免 shell 引号转义问题
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

export PORT="${PORT:-30001}"
export WORKTAB_DATA_DIR="${WORKTAB_DATA_DIR:-/root/.openclaw/workspace/data/my_work_tab}"

# 用 Python 读取 BILI_COOKIE（含单引号等特殊字符），导出给子进程
BILI_COOKIE="$(python3 -c "import io,os; 
v=None
with io.open('.env.bili',encoding='utf-8') as f:
    for line in f:
        line=line.rstrip('\n')
        if line.startswith('BILI_COOKIE='):
            v=line[len('BILI_COOKIE='):]; break
print(v,end='') if v else None")"
export BILI_COOKIE

# 加载混元 Hy Token Plan 配置（HYUNYUAN_API_KEY / HYUNYUAN_BASE_URL / HYUNYUAN_MODEL）
if [[ -f "$DIR/.env" ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    k="$(printf '%s' "$k" | tr -d '\r\n')"
    v="${v%$'\r'}"
    export "$k"="$v"
  done < "$DIR/.env"
fi

exec "$DIR/.venv/bin/python3" "$DIR/app.py"
