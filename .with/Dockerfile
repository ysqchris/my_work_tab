# Multi-stage build for production
FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies (PyMySQL 纯 Python，无需额外编译依赖)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencies stage
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.tencent.com/pypi/simple/ --trusted-host mirrors.tencent.com

# Production stage
FROM base AS runner

# Create non-root user
RUN useradd -m -u 1001 appuser

# Copy dependencies from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application
COPY --chown=appuser:appuser . .

# Environment variables
# 数据库连接（默认值已在 db.py 中兜底，此处声明便于平台注入覆盖）
ENV PORT=8000 \
    DB_HOST=21.63.222.55 \
    DB_PORT=3306 \
    DB_NAME=oeq2zxw6 \
    DB_USER=with_riystfnjvfcqursh \
    DB_PASSWORD="R3k^ANKeJ8xby*" \
    WEREAD_API_KEY="wrk-IGowOUKIS7S0wmkrhhyDzQAA" \
    TAI_IT_TOKEN="" \
    HYUNYUAN_API_KEY="" \
    HYUNYUAN_BASE_URL="https://api.lkeap.cloud.tencent.com/plan/v3" \
    HYUNYUAN_MODEL="hy3" \
    WORKTAB_DATA_DIR="/app/data"

USER appuser

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "120", "app:app"]
