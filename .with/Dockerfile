FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS runner
RUN useradd -m -u 1001 appuser
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY --chown=appuser:appuser . .
RUN python -m py_compile app.py db.py && mkdir -p /app/data/files && chown -R appuser:appuser /app/data

ENV PORT=8000 \
    WORKTAB_DATA_DIR=/app/data \
    IMA_KNOWLEDGE_BASE_NAME="草稿箱" \
    IMA_KNOWLEDGE_BASE_ID=""
# 微信读书：WEREAD_API_KEY（wrk- 开头）必须由运行环境注入，不要写进镜像。
# 缺失时「书影音 → 书籍」会提示「未配置 WEREAD_API_KEY」，不会白屏。

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=3)" || exit 1
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --worker-class gthread --workers 2 --threads 4 --timeout 120 app:app"]
