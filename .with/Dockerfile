FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app
# tzdata：定时任务按北京时间触发（云服务器默认 UTC）
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata && rm -rf /var/lib/apt/lists/*

FROM base AS deps
# 可通过 --build-arg 切换 PyPI 源；腾讯云服务器可用 https://mirrors.cloud.tencent.com/pypi/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

FROM base AS runner
RUN useradd -m -u 1001 appuser
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY --chown=appuser:appuser . .
RUN python -m py_compile app.py db.py auth.py migrate_db.py && mkdir -p /app/data/files && chown -R appuser:appuser /app/data

# 所有凭证（DB_* / APP_PASSWORD / WEREAD_API_KEY / AI_API_KEY 等）都由运行环境注入，不写进镜像。
# 缺 WEREAD_API_KEY 时「书影音 → 书籍」会提示未配置，不会白屏。
ENV PORT=8000 \
    WORKTAB_DATA_DIR=/app/data \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=8 \
    IMA_KNOWLEDGE_BASE_NAME="草稿箱" \
    IMA_KNOWLEDGE_BASE_ID=""

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=3)" || exit 1
# 默认单 worker + 多线程：后台定时任务与内存缓存只保留一份，轻量服务器内存也更省
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --worker-class gthread --workers ${GUNICORN_WORKERS:-1} --threads ${GUNICORN_THREADS:-8} --timeout 120 --forwarded-allow-ips='*' app:app"]
