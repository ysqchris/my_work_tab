"""数据库访问层：MySQL 版 read_json / write_json，供 app.py 无缝替换文件存储。

存储策略：
- 大多数资源（articles / news / knowledge / tasks 等）体量小、读写整份文档，
  存入 json_docs 表（name, data JSON, updated_at），保持"整读整写"语义不变，
  上层 app.py 完全不用感知底层是文件还是数据库。
- creator_videos 体量大（万级行），单独关系化到 creator_videos 表，
  但仍然通过 read_json("creator_videos") / write_json("creator_videos") 的
  统一接口对外暴露，内部转换为对应的行级操作，避免影响业务代码。

连接信息来自 PaaS MySQL 实例，通过环境变量注入，本地开发也可用默认值兜底。
"""
import os
import json
import threading
import datetime

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB

DB_HOST = os.environ.get("DB_HOST", "21.63.222.55")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "oeq2zxw6")
DB_USER = os.environ.get("DB_USER", "with_riystfnjvfcqursh")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "R3k^ANKeJ8xby*")

_pool = None
_pool_lock = threading.Lock()

_CREATOR_VIDEO_STORE = "creator_videos"


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = PooledDB(
                    creator=pymysql,
                    maxconnections=10,
                    mincached=1,
                    maxcached=5,
                    blocking=True,
                    ping=1,
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    charset="utf8mb4",
                    cursorclass=DictCursor,
                    autocommit=True,
                )
    return _pool


def _conn():
    return _get_pool().connection()


def init_schema():
    """幂等建表，服务启动时调用一次即可。"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS json_docs (
                    name VARCHAR(64) NOT NULL PRIMARY KEY,
                    data JSON NOT NULL,
                    updated_at DATETIME NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_videos (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    video_key VARCHAR(64) NOT NULL,
                    creator_id VARCHAR(32) NOT NULL,
                    title TEXT,
                    cover TEXT,
                    url TEXT,
                    published_at BIGINT,
                    stat VARCHAR(128),
                    creator_name VARCHAR(255),
                    creator_avatar TEXT,
                    platform VARCHAR(32),
                    created_at DATETIME,
                    UNIQUE KEY uk_video_key (video_key),
                    KEY idx_creator (creator_id),
                    KEY idx_published (published_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_locks (
                    name VARCHAR(64) NOT NULL,
                    day VARCHAR(16) NOT NULL,
                    expire_at DATETIME NULL,
                    PRIMARY KEY (name, day)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )


# ---------------------------------------------------------------------------
# 通用文档存储：json_docs
# ---------------------------------------------------------------------------
def _read_doc(name, default=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM json_docs WHERE name=%s", (name,))
            row = cur.fetchone()
    if not row:
        return default if default is not None else {"items": []}
    data = row["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return default if default is not None else {"items": []}
    return data


def _write_doc(name, data):
    payload = json.dumps(data, ensure_ascii=False)
    now = datetime.datetime.now()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO json_docs (name, data, updated_at) VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE data=VALUES(data), updated_at=VALUES(updated_at)
                """,
                (name, payload, now),
            )


# ---------------------------------------------------------------------------
# creator_videos 专用关系化存储
# ---------------------------------------------------------------------------
def _video_key(v):
    """用 url 作为去重键；缺 url 时兜底用 title+published_at 拼一个稳定 key。"""
    url = v.get("url")
    if url:
        return url[:64] if len(url) <= 64 else __import__("hashlib").md5(url.encode("utf-8")).hexdigest()
    raw = f"{v.get('title','')}_{v.get('published_at','')}"
    return __import__("hashlib").md5(raw.encode("utf-8")).hexdigest()


def _read_creator_videos():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT video_key, creator_id, title, cover, url, published_at, stat,
                       creator_name, creator_avatar, platform, created_at
                FROM creator_videos
                ORDER BY published_at DESC
                """
            )
            rows = cur.fetchall()
    items = []
    latest = None
    for r in rows:
        items.append({
            "creator_id": r["creator_id"],
            "title": r["title"],
            "cover": r["cover"],
            "url": r["url"],
            "published_at": r["published_at"],
            "stat": r["stat"],
            "creator_name": r["creator_name"],
            "creator_avatar": r["creator_avatar"],
            "platform": r["platform"],
        })
        if r["created_at"] and (latest is None or r["created_at"] > latest):
            latest = r["created_at"]
    return {"items": items, "updated_at": latest.isoformat() if latest else None}


def _write_creator_videos(data):
    """全量覆盖式写入：按 video_key upsert，语义等价于原来的整份文件覆盖写。"""
    items = (data or {}).get("items") or []
    now = datetime.datetime.now()
    with _conn() as conn:
        with conn.cursor() as cur:
            for v in items:
                key = _video_key(v)
                cur.execute(
                    """
                    INSERT INTO creator_videos
                        (video_key, creator_id, title, cover, url, published_at, stat,
                         creator_name, creator_avatar, platform, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        creator_id=VALUES(creator_id), title=VALUES(title), cover=VALUES(cover),
                        url=VALUES(url), published_at=VALUES(published_at), stat=VALUES(stat),
                        creator_name=VALUES(creator_name), creator_avatar=VALUES(creator_avatar),
                        platform=VALUES(platform)
                    """,
                    (
                        key,
                        v.get("creator_id"),
                        v.get("title"),
                        v.get("cover"),
                        v.get("url"),
                        v.get("published_at"),
                        v.get("stat"),
                        v.get("creator_name"),
                        v.get("creator_avatar"),
                        v.get("platform"),
                        now,
                    ),
                )


# ---------------------------------------------------------------------------
# 统一入口：供 app.py 的 read_json / write_json 调用
# ---------------------------------------------------------------------------
def db_read_json(name, default=None):
    if name == _CREATOR_VIDEO_STORE:
        return _read_creator_videos()
    return _read_doc(name, default)


def db_write_json(name, data):
    if name == _CREATOR_VIDEO_STORE:
        _write_creator_videos(data)
        return
    _write_doc(name, data)


def try_acquire_daily_lock(name, day, ttl_seconds=3600):
    """跨进程原子抢占"每日任务锁"。

    用于 gunicorn 多 worker 部署下，保证同一任务每天只有一个 worker 真正执行。
    返回 True 表示抢到锁（可执行）；False 表示已被其他进程抢到。

    :param name: 任务名（如 'imate_ops'）
    :param day: 日期标识（如 '2026-08-29'），同一天只允许一次
    :param ttl_seconds: 锁有效期，超时后可被清理重抢
    """
    now = datetime.datetime.now()
    expire = now + datetime.timedelta(seconds=ttl_seconds)
    with _conn() as conn:
        with conn.cursor() as cur:
            # 清理已过期的锁，避免异常退出后死锁
            cur.execute("DELETE FROM daily_locks WHERE name=%s AND expire_at < NOW()", (name,))
            try:
                cur.execute(
                    "INSERT INTO daily_locks (name, day, expire_at) VALUES (%s, %s, %s)",
                    (name, day, expire),
                )
                return True
            except pymysql.err.IntegrityError:
                return False
