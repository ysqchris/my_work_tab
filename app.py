#!/usr/bin/env python3
"""工作台 — 本地信息聚合站后端

提供：
  - 静态页面（index.html）
  - 数据读写 API（articles / products_updates / products）
数据存储在 data/*.json，所有写入走原子写 + 文件锁，避免 cron 与网页并发冲突。
"""
import json
import os
import threading
import uuid
import datetime
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
FILES = os.path.join(DATA, "files")
STATIC = os.path.join(BASE, "static")

app = Flask(__name__, static_folder=STATIC, template_folder=os.path.join(BASE, "templates"))
os.makedirs(FILES, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64MB

# 文件锁，防止并发写损坏 JSON
_locks = {}
_locks_lock = threading.Lock()

def _lock_for(name):
    with _locks_lock:
        if name not in _locks:
            _locks[name] = threading.Lock()
        return _locks[name]

def _now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")

def _path(name):
    return os.path.join(DATA, name + ".json")

def read_json(name, default=None):
    p = _path(name)
    if not os.path.exists(p):
        return default if default is not None else {"items": []}
    with open(p, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return default if default is not None else {"items": []}

def write_json(name, data):
    """原子写：先写临时文件再 rename，避免半截写入。"""
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def touch_meta():
    meta = read_json("meta", {"site_name": "工作台"})
    meta["updated_at"] = _now()
    write_json("meta", meta)

# ---------------------------------------------------------------------------
# 通用 CRUD 工厂
# ---------------------------------------------------------------------------
def make_crud(resource):
    """resource: 'articles' | 'products_updates' 等，数据在 data[items] 列表。"""
    R = resource  # 闭包变量，用于端点名区分

    @app.route(f"/api/{R}", endpoint=f"list_{R}", methods=["GET"])
    def list_items():
        data = read_json(R, {"items": []})
        # 兜底：确保每条都有稳定 id（缺则用 url 派生，保证前端编辑/删除可用）
        changed = False
        for it in data["items"]:
            if not it.get("id"):
                it["id"] = uuid.uuid5(uuid.NAMESPACE_URL, str(it.get("url") or it.get("title") or uuid.uuid4().hex)).hex[:12]
                changed = True
        if changed:
            with _lock_for(R):
                write_json(R, data)
        return jsonify(data)

    @app.route(f"/api/{R}", endpoint=f"create_{R}", methods=["POST"])
    def create_item():
        data = read_json(R, {"items": []})
        body = request.get_json(force=True, silent=True) or {}
        item = dict(body)
        item["id"] = item.get("id") or uuid.uuid4().hex[:12]
        item["created_at"] = _now()
        if "date" not in item and "date_collected" not in item:
            item["date"] = _now()[:10]
        if R == "knowledge":
            item["kind"] = item.get("kind") or (
                "file" if item.get("stored_name") else
                "document" if item.get("content") and not item.get("url") else
                "link"
            )
        data["items"].insert(0, item)
        data["updated_at"] = _now()
        with _lock_for(R):
            write_json(R, data)
        touch_meta()
        return jsonify(item), 201

    @app.route(f"/api/{R}/<item_id>", endpoint=f"update_{R}", methods=["PUT", "PATCH"])
    def update_item(item_id):
        data = read_json(R, {"items": []})
        body = request.get_json(force=True, silent=True) or {}
        for i, it in enumerate(data["items"]):
            if str(it.get("id")) == str(item_id):
                it.update(body)
                it["id"] = it.get("id") or item_id
                data["items"][i] = it
                data["updated_at"] = _now()
                with _lock_for(R):
                    write_json(R, data)
                touch_meta()
                return jsonify(it)
        return jsonify({"error": "not found"}), 404

    @app.route(f"/api/{R}/<item_id>", endpoint=f"delete_{R}", methods=["DELETE"])
    def delete_item(item_id):
        data = read_json(R, {"items": []})
        removed = [it for it in data["items"] if str(it.get("id")) == str(item_id)]
        if not removed:
            return jsonify({"error": "not found"}), 404
        data["items"] = [it for it in data["items"] if str(it.get("id")) != str(item_id)]
        data["updated_at"] = _now()
        with _lock_for(R):
            write_json(R, data)
        if R == "knowledge":
            for it in removed:
                _delete_knowledge_file(it)
        touch_meta()
        return jsonify({"ok": True})

# 通用内容对象：情报、行动、知识库与思考均可独立存储。
make_crud("articles")
make_crud("products_updates")
make_crud("tasks")
make_crud("notes")
make_crud("knowledge")
make_crud("inbox")

# ---------------------------------------------------------------------------
# 知识库：链接 / 文档 / 文件；旧 links.json 自动迁移
# ---------------------------------------------------------------------------
def _infer_knowledge_kind(it):
    if it.get("kind") in ("link", "document", "file"):
        return it["kind"]
    if it.get("stored_name") or it.get("filename"):
        return "file"
    if it.get("content") and not it.get("url"):
        return "document"
    return "link"

def _delete_knowledge_file(it):
    name = it.get("stored_name")
    if not name:
        return
    path = os.path.join(FILES, name)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass

def _migrate_links_to_knowledge():
    kp, lp = _path("knowledge"), _path("links")
    if os.path.exists(kp):
        data = read_json("knowledge", {"items": []})
        changed = False
        for it in data.get("items", []):
            kind = _infer_knowledge_kind(it)
            if it.get("kind") != kind:
                it["kind"] = kind
                changed = True
        if changed:
            with _lock_for("knowledge"):
                write_json("knowledge", data)
        return
    if not os.path.exists(lp):
        write_json("knowledge", {"items": [], "updated_at": _now()})
        return
    data = read_json("links", {"items": []})
    for it in data.get("items", []):
        it["kind"] = _infer_knowledge_kind(it)
    data["updated_at"] = _now()
    write_json("knowledge", data)

_migrate_links_to_knowledge()

def _save_upload_file(f, default_name="paste.bin"):
    """保存上传文件；兼容剪贴板无文件名的 blob。"""
    if not f:
        return None
    original = (f.filename or "").strip() or default_name
    # 按 mime 补扩展名，避免 secure_filename 把空名吃掉
    mime = (f.mimetype or "").lower()
    if "." not in original:
        ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }.get(mime, "")
        original = (original if original != "blob" else "paste") + ext
    safe = secure_filename(original) or ("paste" + (".png" if mime.startswith("image/") else ".bin"))
    stored = f"{uuid.uuid4().hex[:12]}_{safe}"
    path = os.path.join(FILES, stored)
    f.save(path)
    return {
        "stored_name": stored,
        "filename": original,
        "mime": f.mimetype or "application/octet-stream",
        "size": os.path.getsize(path),
    }

@app.route("/api/upload", methods=["POST"])
def generic_upload():
    """通用文件上传（如待办/记录配图），只存文件、不落库，返回 stored_name。"""
    f = request.files.get("file")
    saved = _save_upload_file(f, "paste.png")
    if not saved:
        return jsonify({"error": "file required"}), 400
    return jsonify(saved), 201

@app.route("/api/knowledge/upload", methods=["POST"])
def upload_knowledge_file():
    """上传文件到知识库。form-data: file, 可选 topic/title。"""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    original = f.filename
    safe = secure_filename(original) or "file"
    stored = f"{uuid.uuid4().hex[:12]}_{safe}"
    path = os.path.join(FILES, stored)
    f.save(path)
    size = os.path.getsize(path)
    title = (request.form.get("title") or "").strip() or original
    topic = (request.form.get("topic") or "").strip()
    item = {
        "id": uuid.uuid4().hex[:12],
        "kind": "file",
        "title": title,
        "filename": original,
        "stored_name": stored,
        "mime": f.mimetype or "application/octet-stream",
        "size": size,
        "topic": topic,
        "source": "本地上传",
        "created_at": _now(),
        "date": _now()[:10],
    }
    data = read_json("knowledge", {"items": []})
    data["items"].insert(0, item)
    data["updated_at"] = _now()
    with _lock_for("knowledge"):
        write_json("knowledge", data)
    touch_meta()
    return jsonify(item), 201

def _is_image_meta(meta, filename=""):
    mime = (meta or {}).get("mime") or ""
    name = (meta or {}).get("filename") or filename or ""
    if mime.startswith("image/"):
        return True
    return name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"))

@app.route("/api/knowledge/files/<stored_name>", methods=["GET"])
def download_knowledge_file(stored_name):
    """预览或下载知识库文件；图片默认内联展示，?download=1 强制下载。"""
    safe = os.path.basename(stored_name)
    path = os.path.join(FILES, safe)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    data = read_json("knowledge", {"items": []})
    meta = next((it for it in data.get("items", []) if it.get("stored_name") == safe), None)
    force_download = request.args.get("download") == "1"
    return send_from_directory(
        FILES,
        safe,
        as_attachment=force_download or not _is_image_meta(meta, safe),
        download_name=(meta or {}).get("filename") or safe,
    )

# ---------------------------------------------------------------------------
# products（好品集）— 扁平结构 {products: [...]}，
# 每个产品带 type 标签（展示分类）与 source 字段（采集路径）。
# 读取时向后兼容旧 {groups:{...}} 格式，自动转换为扁平结构。
# ---------------------------------------------------------------------------
def _normalize_products(raw):
    """兼容旧 groups 结构，统一返回 {'products': [...]}。"""
    if isinstance(raw, dict) and "products" in raw and isinstance(raw["products"], list):
        return {"products": raw["products"]}
    if isinstance(raw, dict) and "groups" in raw:
        out = []
        for g, gd in raw["groups"].items():
            ps = gd.get("products", []) if isinstance(gd, dict) else []
            for p in ps:
                p = dict(p)
                p.setdefault("type", g)
                p.setdefault("source", gd.get("source", "web"))
                out.append(p)
        return {"products": out}
    if isinstance(raw, list):
        return {"products": raw}
    return {"products": []}

def _migrate_if_needed():
    """若磁盘上是旧 groups 格式，就地转换为扁平结构写回。"""
    p = _path("products")
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return
    if isinstance(raw, dict) and "groups" in raw:
        write_json("products", _normalize_products(raw))

@app.route("/api/products", methods=["GET"])
def get_products():
    _migrate_if_needed()
    return jsonify(_normalize_products(read_json("products", {"products": []})))

@app.route("/api/products", methods=["POST"])
def add_product():
    """新增产品。body: {type, name, company, source, keywords, official_url, note}"""
    _migrate_if_needed()
    data = _normalize_products(read_json("products", {"products": []}))
    body = request.get_json(force=True, silent=True) or {}
    ptype = body.get("type", "").strip()
    if not ptype:
        return jsonify({"error": "type required"}), 400
    prod = {
        "id": body.get("id") or uuid.uuid4().hex[:10],
        "name": body.get("name", "未命名"),
        "company": body.get("company", ""),
        "type": ptype,
        "source": body.get("source", "web"),
        "keywords": body.get("keywords", []),
        "official_url": body.get("official_url", ""),
        "note": body.get("note", ""),
    }
    if "km_knowledge_id" in body:
        prod["km_knowledge_id"] = body["km_knowledge_id"]
    if "km_knowledge_url" in body:
        prod["km_knowledge_url"] = body["km_knowledge_url"]
    data["products"].append(prod)
    with _lock_for("products"):
        write_json("products", data)
    touch_meta()
    return jsonify(prod), 201

@app.route("/api/products/<prod_id>", methods=["PUT", "PATCH"])
def update_product(prod_id):
    _migrate_if_needed()
    data = _normalize_products(read_json("products", {"products": []}))
    body = request.get_json(force=True, silent=True) or {}
    for i, p in enumerate(data["products"]):
        if str(p.get("id")) == str(prod_id):
            # 改 type 只更新标签，不跨组迁移
            p.update({k: v for k, v in body.items() if k not in ("group",)})
            data["products"][i] = p
            with _lock_for("products"):
                write_json("products", data)
            touch_meta()
            return jsonify(p)
    return jsonify({"error": "not found"}), 404

@app.route("/api/products/<prod_id>", methods=["DELETE"])
def delete_product(prod_id):
    _migrate_if_needed()
    data = _normalize_products(read_json("products", {"products": []}))
    before = len(data["products"])
    data["products"] = [p for p in data["products"] if str(p.get("id")) != str(prod_id)]
    if len(data["products"]) != before:
        with _lock_for("products"):
            write_json("products", data)
        touch_meta()
        return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

@app.route("/api/types", methods=["GET"])
def list_types():
    """返回所有已使用的 type 标签（去重，保序）。"""
    _migrate_if_needed()
    data = _normalize_products(read_json("products", {"products": []}))
    seen = []
    for p in data["products"]:
        t = p.get("type", "")
        if t and t not in seen:
            seen.append(t)
    return jsonify(seen)

# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(STATIC, "favicon-32.png", mimetype="image/png")

@app.route("/api/meta", methods=["GET"])
def get_meta():
    return jsonify(read_json("meta", {}))

@app.route("/healthz")
def healthz():
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    app.run(host="0.0.0.0", port=port, debug=False)
