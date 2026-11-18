"""数据迁移：把工作台数据从一个 MySQL 导出为 JSON 文件，再导入到另一个 MySQL。

典型用法（内网库 -> 云服务器上的库）：

  1) 在能连上旧库的机器上导出（读取 .env / 环境变量里的 DB_*）：
       python migrate_db.py export worktab-dump.json
  2) 把文件传到云服务器，导入到 compose 里的 MySQL：
       docker compose cp worktab-dump.json app:/tmp/dump.json
       docker compose exec app python migrate_db.py import /tmp/dump.json

导入是幂等的（按主键 upsert），重复执行不会产生重复数据。
涵盖表：json_docs（绝大多数业务数据）、creator_videos、service_credentials。
上传的知识库文件在 WORKTAB_DATA_DIR/files 下，需另行拷贝（见 DEPLOY 说明）。
"""
import os
import sys
import json
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    path = os.path.join(BASE, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and not os.environ.get(k, "").strip():
                os.environ[k] = v


_load_dotenv()
import db  # noqa: E402  需在加载 .env 之后导入

TABLES = {
    "json_docs": ["name", "data", "updated_at"],
    "creator_videos": ["video_key", "creator_id", "title", "cover", "url", "published_at", "stat",
                       "creator_name", "creator_avatar", "platform", "created_at"],
    "service_credentials": ["service", "client_id", "api_key", "updated_at"],
}


def _jsonable(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat(sep=" ") if isinstance(v, datetime.datetime) else v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="ignore")
    return v


def export_to(path):
    out = {"version": 1, "exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "tables": {}}
    with db._conn() as conn:
        with conn.cursor() as cur:
            for table, cols in TABLES.items():
                try:
                    cur.execute("SELECT %s FROM %s" % (", ".join(cols), table))
                    rows = cur.fetchall()
                except Exception as e:
                    print(f"  跳过 {table}: {e}")
                    continue
                out["tables"][table] = [{c: _jsonable(r.get(c)) for c in cols} for r in rows]
                print(f"  {table}: {len(rows)} 行")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"已导出到 {path}（{os.path.getsize(path) // 1024} KB）。文件含凭证，传输后请删除。")


def import_from(path):
    with open(path, encoding="utf-8") as f:
        dump = json.load(f)
    db.init_schema()
    with db._conn() as conn:
        with conn.cursor() as cur:
            for table, cols in TABLES.items():
                rows = (dump.get("tables") or {}).get(table) or []
                if not rows:
                    continue
                placeholders = ", ".join(["%s"] * len(cols))
                updates = ", ".join(f"{c}=VALUES({c})" for c in cols[1:])
                sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
                batch = []
                for r in rows:
                    vals = []
                    for c in cols:
                        v = r.get(c)
                        if c == "data" and not isinstance(v, str):
                            v = json.dumps(v, ensure_ascii=False)
                        vals.append(v)
                    batch.append(vals)
                for i in range(0, len(batch), 500):
                    cur.executemany(sql, batch[i:i + 500])
                print(f"  {table}: 导入 {len(batch)} 行")
    print("导入完成。重启应用后生效：docker compose restart app")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "import"):
        print(__doc__)
        sys.exit(1)
    cmd, path = sys.argv[1], sys.argv[2]
    cfg = db.db_config()
    print(f"{'导出自' if cmd == 'export' else '导入到'} {cfg['host']}:{cfg['port']}/{cfg['database']}")
    (export_to if cmd == "export" else import_from)(path)


if __name__ == "__main__":
    main()
