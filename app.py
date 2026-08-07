#!/usr/bin/env python3
"""工作台 — 本地信息聚合站后端

提供：
  - 静态页面（index.html）
  - 数据读写 API（articles / products_updates / products）
数据存储在 data/*.json，所有写入走原子写 + 文件锁，避免 cron 与网页并发冲突。
"""
import json
import os
import re
import html
import threading
import uuid
import datetime
import urllib.request
import urllib.parse
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
# 用户数据放在代码仓库之外的持久目录；未配置时兼容本地开发。
DATA = os.environ.get("WORKTAB_DATA_DIR", os.path.join(BASE, "data"))
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
        if R == "bookmarks":
            # 内网 <站点>.woa.com 书签，若本地已有对应图标文件则自动纠正（既可修新书签，旧书签图标固定后也能跟着生效）
            for it in data["items"]:
                url = it.get("url") or ""
                if not url:
                    continue
                override_icon = _domain_icon_override(url) or _woa_site_icon(url)
                if override_icon and it.get("favicon") != override_icon:
                    it["favicon"] = override_icon
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
make_crud("bookmarks")

# ---------------------------------------------------------------------------
# 书影音：豆瓣热门代理（须在 media CRUD 之前注册，避免被 <id> 吃掉）
# ---------------------------------------------------------------------------
_DOUBAN_HOT_TYPES = {
    "movie": "movie_real_time_hotest",
    "tv": "tv_real_time_hotest",
    "show": "show_chinese_best_weekly",
    "music": "music_single",
    "book": "book_hot",
}
_DOUBAN_DOMAIN = {
    "movie": "av", "tv": "av", "show": "av", "music": "av", "book": "book",
}
_douban_hot_cache = {}
_douban_hot_lock = threading.Lock()
_DOUBAN_HOT_CACHE_NAME = "douban_hot"


# ---------------------------------------------------------------------------
# 天气：代理 weatherapi.com，前端小时钟/天气小标题用，缓存 20 分钟避免频繁请求。
# ---------------------------------------------------------------------------
_WEATHER_API_KEY = "d92b0294d0ee41e587933457260603"
_weather_cache = {"data": None, "ts": 0}
_weather_lock = threading.Lock()


@app.route("/api/weather", methods=["GET"])
def get_weather():
    city = (request.args.get("city") or "深圳").strip()
    now_ts = datetime.datetime.now().timestamp()
    with _weather_lock:
        cached = _weather_cache.get("data")
        if cached and cached.get("city") == city and now_ts - _weather_cache.get("ts", 0) < 1200:
            return jsonify(cached)
    try:
        import ssl as _ssl
        params = urllib.parse.urlencode({"q": city, "days": "1", "key": _WEATHER_API_KEY})
        url = f"https://api.weatherapi.com/v1/forecast.json?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        ctx = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        result = {
            "city": city,
            "temp_c": payload["current"]["temp_c"],
            "condition": payload["current"]["condition"]["text"],
            "icon": payload["current"]["condition"].get("icon", ""),
            "error": None,
        }
        with _weather_lock:
            _weather_cache["data"] = result
            _weather_cache["ts"] = now_ts
        return jsonify(result)
    except Exception as e:
        stale = _weather_cache.get("data")
        if stale:
            return jsonify(stale)
        return jsonify({"city": city, "temp_c": None, "condition": "", "icon": "", "error": str(e)})


def _shanghai_date():
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).date().isoformat()


def _load_douban_hot_cache():
    """读取每天中午任务写入的持久缓存，服务重启后仍可复用。"""
    saved = read_json(_DOUBAN_HOT_CACHE_NAME, {"date": "", "items": {}})
    if not isinstance(saved, dict) or not isinstance(saved.get("items"), dict):
        return {"date": "", "items": {}}
    return saved


def _save_douban_hot_cache(cache):
    write_json(_DOUBAN_HOT_CACHE_NAME, cache)

def _normalize_douban_item(raw, subtype):
    """把 Rexxar subject 条目映射成前端统一结构。"""
    if not isinstance(raw, dict):
        return None
    # collection items 常包一层 subject
    sub = raw.get("subject") if isinstance(raw.get("subject"), dict) else raw
    douban_id = str(sub.get("id") or raw.get("id") or "").strip()
    title = (sub.get("title") or raw.get("title") or "").strip()
    if not title and not douban_id:
        return None
    rating = sub.get("rating") or raw.get("rating") or {}
    if isinstance(rating, dict):
        score = rating.get("value") or rating.get("average") or None
    else:
        score = rating
    try:
        score = float(score) if score not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        score = None
    cover = ""
    for key in ("cover", "pic", "images"):
        val = sub.get(key) or raw.get(key)
        if isinstance(val, dict):
            cover = (
                val.get("large") or val.get("normal") or val.get("medium")
                or val.get("small") or val.get("url")
            )
        elif isinstance(val, str):
            cover = val
        if cover:
            break
    url = (sub.get("url") or raw.get("url") or "").strip()
    if not url and douban_id:
        if subtype == "book":
            url = f"https://book.douban.com/subject/{douban_id}/"
        elif subtype == "music":
            url = f"https://music.douban.com/subject/{douban_id}/"
        else:
            url = f"https://movie.douban.com/subject/{douban_id}/"
    year = str(sub.get("year") or raw.get("year") or "").strip()
    card_subtitle = (
        sub.get("card_subtitle")
        or raw.get("card_subtitle")
        or sub.get("subtitle")
        or ""
    )
    if isinstance(card_subtitle, list):
        card_subtitle = " / ".join(str(x) for x in card_subtitle if x)
    return {
        "douban_id": douban_id,
        "title": title or douban_id,
        "cover": cover,
        "rating": score,
        "url": url,
        "year": year,
        "card_subtitle": str(card_subtitle).strip(),
        "subtype": subtype,
        "domain": _DOUBAN_DOMAIN.get(subtype, "av"),
        "source": "douban",
    }

def _fetch_douban_collection(collection, count=20):
    url = (
        f"https://m.douban.com/rexxar/api/v2/subject_collection/{collection}/items"
        f"?start=0&count={count}&items_only=1&for_mobile=1"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Referer": f"https://m.douban.com/subject_collection/{collection}",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    items = payload.get("subject_collection_items") or payload.get("items") or []
    return items if isinstance(items, list) else []

@app.route("/api/media/douban-hot", methods=["GET"])
def media_douban_hot():
    """返回每日中午更新一次的豆瓣热门榜；首次运行才按需初始化。"""
    subtype = (request.args.get("type") or "movie").strip().lower()
    if subtype not in _DOUBAN_HOT_TYPES:
        return jsonify({"error": "invalid type", "items": []}), 400

    # refresh=1 仅由每日 12:00 的本地定时任务调用。
    force_refresh = request.args.get("refresh") == "1"
    today = _shanghai_date()
    with _douban_hot_lock:
        saved = _load_douban_hot_cache()
        cached_items = saved.get("items", {}).get(subtype)
        # 页面访问始终只读取缓存，不再每 30 分钟向豆瓣发请求。
        if cached_items is not None and not force_refresh:
            return jsonify({"items": cached_items, "cached": True, "updated_at": saved.get("updated_at"), "error": None})
        # 首次部署尚无缓存时，允许初始化一次，避免页面空白。
        if cached_items is not None and not force_refresh and saved.get("date") == today:
            return jsonify({"items": cached_items, "cached": True, "updated_at": saved.get("updated_at"), "error": None})

    error = None
    items = []
    try:
        raw_items = _fetch_douban_collection(_DOUBAN_HOT_TYPES[subtype])
        for raw in raw_items:
            mapped = _normalize_douban_item(raw, subtype)
            if mapped:
                items.append(mapped)
    except Exception as e:
        error = str(e)

    with _douban_hot_lock:
        saved = _load_douban_hot_cache()
        if items:
            saved.setdefault("items", {})[subtype] = items
            saved["date"] = today
            saved["updated_at"] = _now()
            _save_douban_hot_cache(saved)
            return jsonify({"items": items, "cached": False, "updated_at": saved["updated_at"], "error": None})
        # 更新失败时继续返回上一版缓存，页面不受影响。
        stale = saved.get("items", {}).get(subtype, [])
        return jsonify({"items": stale, "cached": True, "updated_at": saved.get("updated_at"), "error": error})

make_crud("media")

# ---------------------------------------------------------------------------
# 豆瓣封面图片代理：豆瓣图片域名对无 Referer/UA 的请求返回 418，
# 前端直连会被拦截导致封面加载失败，这里由后端转发一次。
# ---------------------------------------------------------------------------
from flask import Response

_ALLOWED_IMG_HOST_SUFFIX = ".doubanio.com"


@app.route("/api/media/img-proxy", methods=["GET"])
def media_img_proxy():
    src = (request.args.get("url") or "").strip()
    if not src:
        return "missing url", 400
    try:
        parsed = urllib.parse.urlparse(src)
    except Exception:
        return "bad url", 400
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not (host == "doubanio.com" or host.endswith(_ALLOWED_IMG_HOST_SUFFIX)):
        return "host not allowed", 400
    req = urllib.request.Request(src, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Referer": "https://movie.douban.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        return f"fetch failed: {e}", 502
    return Response(data, mimetype=content_type, headers={
        "Cache-Control": "public, max-age=86400",
    })

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

# ---------------------------------------------------------------------------
# 书签：抓取网页 <title> 和 favicon，新建书签时自动填充
# ---------------------------------------------------------------------------
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
_LINK_TAG_RE = re.compile(rb"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(rb'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["\']([^"\']*)["\']')
_ICON_RELS = {"icon", "shortcut icon", "apple-touch-icon", "apple-touch-icon-precomposed"}

def _extract_favicon(raw):
    """逐个解析 <link> 标签的属性字典再取 rel/href，避免 data-base-href 这类属性名里也带 'href=' 而被误匹配。"""
    for tag in _LINK_TAG_RE.findall(raw):
        attrs = {}
        for am in _ATTR_RE.finditer(tag):
            attrs[am.group(1).decode("ascii", "ignore").lower()] = am.group(2)
        rel = (attrs.get("rel") or b"").decode("utf-8", "ignore").strip().lower()
        href = attrs.get("href")
        if rel in _ICON_RELS and href:
            return href.decode("utf-8", "ignore").strip()
    return None

def _extract_summary(raw):
    """从 <meta name="description"> 或 og:description 抽一句话摘要，抽不到就留空，不阀阻创建。"""
    best = None
    for tag in _META_TAG_RE.findall(raw):
        attrs = {}
        for am in _ATTR_RE.finditer(tag):
            attrs[am.group(1).decode("ascii", "ignore").lower()] = am.group(2)
        name = (attrs.get("name") or attrs.get("property") or b"").decode("utf-8", "ignore").strip().lower()
        content = attrs.get("content")
        if not content:
            continue
        text = html.unescape(content.decode("utf-8", "ignore")).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        if name == "description":
            return text[:160]
        if name in ("og:description", "twitter:description") and not best:
            best = text[:160]
    return best or ""


# 内网 SSO 登录相关域名/标题关键词：服务端抓取时没有用户浏览器的登录态，
# 需要登录的内网页面（如 git.woa.com 仓库）会被重定向到这些域名，
# 抓到的 <title> 只会是登录页标题（如“OA登录”），不能当作真实标题使用。
_SSO_HOST_HINTS = ("passport.", "login.", "sso.", "oa.", "auth.")
_SSO_TITLE_HINTS = ("oa登录", "登录", "login", "sign in", "signin", "统一身份认证", "passport")


def _looks_like_sso_redirect(final_url, title):
    """判断抓取结果是否命中了 SSO/登录跳转页，而不是目标页面本身。"""
    host = urllib.parse.urlsplit(final_url).netloc.lower()
    if any(hint in host for hint in _SSO_HOST_HINTS):
        return True
    t = (title or "").strip().lower()
    if t and any(hint in t for hint in _SSO_TITLE_HINTS) and len(t) <= 12:
        return True
    return False


def _fallback_title_from_url(url):
    """当抓取到的标题不可信（命中登录页）时，从 URL 路径里拼一个可读的兜底标题。
    例如 https://git.woa.com/Design/imate_project_cooperation -> "Design/imate_project_cooperation · git.woa.com"
    """
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.strip("/")
    path = urllib.parse.unquote(path)
    if path:
        return f"{path} · {parsed.netloc}"
    return parsed.netloc or url


# 特定域名的书签固定使用本地静态图标，不依赖实时抓取（内网站点常因登录态/网络策略导致favicon抓取失败或不清晰）。
# key 为域名后缀匹配（endswith），value 为 /static/ 下的图标路径。
_DOMAIN_ICON_OVERRIDES = {
    "git.woa.com": "/static/bookmark-icons/gongfeng.png",  # 工蜂仓库，用户指定固定图标
}


def _woa_site_icon(url):
    """通用规则：内网 <站点名>.woa.com 的链接，若本地已有 static/bookmark-icons/<站点名>.png 图标，直接用它。
    例如 with.woa.com -> static/bookmark-icons/with.png；km.woa.com -> .../km.png。
    比 _DOMAIN_ICON_OVERRIDES 更通用，不需要逐个域名手动登记，只要放对应文件名的图标进 bookmark-icons/ 目录即可生效。
    """
    host = urllib.parse.urlsplit(url).netloc.lower()
    if not host.endswith(".woa.com"):
        return None
    site = host[: -len(".woa.com")]
    # 去掉常见的二级子域前缀干扰（如 portal.learn -> learn），只取最后一段作为站点名
    site = site.rsplit(".", 1)[-1]
    if not site:
        return None
    icon_path = os.path.join(STATIC, "bookmark-icons", f"{site}.png")
    if os.path.isfile(icon_path):
        return f"/static/bookmark-icons/{site}.png"
    return None


def _domain_icon_override(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    for domain, icon in _DOMAIN_ICON_OVERRIDES.items():
        if host == domain or host.endswith("." + domain):
            return icon
    return None


@app.route("/api/bookmarks/fetch-meta", methods=["GET"])
def fetch_bookmark_meta():
    """抓取网页标题和 favicon，用于新建书签自动填充。抓取失败时静默降级（返回空标题+兜底图标），不阻塞创建。
    内网需要登录的页面，服务端请求没有用户浏览器的登录 Cookie，会被重定向到 SSO 登录页；
    此时抓到的标题（如“OA登录”）不可信，改用 URL 路径拼出的兜底标题，避免误导用户。
    """
    url = (request.args.get("url") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "invalid url"}), 400
    parsed = urllib.parse.urlsplit(url)
    favicon = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    title = ""
    summary = ""
    final_url = url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        })
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read(300 * 1024)
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                import gzip as _gzip
                try:
                    raw = _gzip.decompress(raw)
                except Exception:
                    pass
            charset = resp.headers.get_content_charset() or "utf-8"
            final_url = resp.geturl() or url
        m = _TITLE_RE.search(raw)
        if m:
            title = html.unescape(m.group(1).decode(charset, errors="ignore"))
            title = re.sub(r"\s+", " ", title).strip()
        summary = _extract_summary(raw)
        href = _extract_favicon(raw)
        if href:
            favicon = urllib.parse.urljoin(url, href)
    except Exception:
        pass
    if _looks_like_sso_redirect(final_url, title):
        title = _fallback_title_from_url(url)
        summary = ""
    override_icon = _domain_icon_override(url) or _woa_site_icon(url)
    if override_icon:
        favicon = override_icon
    return jsonify({"title": title, "favicon": favicon, "summary": summary})


@app.route("/api/bookmarks/reorder", methods=["POST"])
def reorder_bookmarks():
    """拖拽排序后持久化。请求体 {"ids": ["id1","id2",...]} 是同一 kind（常用/备忘）子集内的新顺序。
    实现方式：在原数组中找到这些 id 首次出现的位置，整体替换为新顺序，其他 kind 的元素保持原有相对位置不变。
    """
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids required"}), 400
    data = read_json("bookmarks", {"items": []})
    items = data["items"]
    by_id = {str(it.get("id")): it for it in items}
    ids = [str(i) for i in ids if str(i) in by_id]
    if not ids:
        return jsonify({"error": "no matching ids"}), 400
    id_set = set(ids)
    new_items = []
    inserted = False
    for it in items:
        iid = str(it.get("id"))
        if iid in id_set:
            if not inserted:
                new_items.extend(by_id[i] for i in ids)
                inserted = True
            # 已在上面一次性插入过，跳过重复
        else:
            new_items.append(it)
    if not inserted:
        new_items.extend(by_id[i] for i in ids)
    data["items"] = new_items
    data["updated_at"] = _now()
    with _lock_for("bookmarks"):
        write_json("bookmarks", data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 书签分组：支持新建/重命名/删除，首次请求时自动初始化默认分组（内部/外部）。
# 删除分组时需要同步清除书签中引用该分组的 group 字段，回退到“未分组”，所以不用通用 make_crud。
# ---------------------------------------------------------------------------

def _default_bookmark_groups():
    now = _now()
    return {
        "items": [
            {"id": "internal", "name": "内部", "created_at": now},
            {"id": "external", "name": "外部", "created_at": now},
        ],
        "updated_at": now,
    }


@app.route("/api/bookmark-groups", methods=["GET"])
def list_bookmark_groups():
    data = read_json("bookmark_groups", None)
    if not data or not data.get("items"):
        data = _default_bookmark_groups()
        with _lock_for("bookmark_groups"):
            write_json("bookmark_groups", data)
    return jsonify(data)


@app.route("/api/bookmark-groups", methods=["POST"])
def create_bookmark_group():
    data = read_json("bookmark_groups", None) or _default_bookmark_groups()
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    item = {"id": uuid.uuid4().hex[:10], "name": name, "created_at": _now()}
    data["items"].append(item)
    data["updated_at"] = _now()
    with _lock_for("bookmark_groups"):
        write_json("bookmark_groups", data)
    return jsonify(item), 201


@app.route("/api/bookmark-groups/<group_id>", methods=["PUT", "PATCH"])
def update_bookmark_group(group_id):
    data = read_json("bookmark_groups", None) or _default_bookmark_groups()
    for i, it in enumerate(data["items"]):
        if str(it.get("id")) == str(group_id):
            body = request.get_json(force=True, silent=True) or {}
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not name:
                    return jsonify({"error": "name required"}), 400
                it["name"] = name
            data["items"][i] = it
            data["updated_at"] = _now()
            with _lock_for("bookmark_groups"):
                write_json("bookmark_groups", data)
            return jsonify(it)
    return jsonify({"error": "not found"}), 404


@app.route("/api/bookmark-groups/<group_id>", methods=["DELETE"])
def delete_bookmark_group(group_id):
    data = read_json("bookmark_groups", None) or _default_bookmark_groups()
    before = len(data["items"])
    data["items"] = [it for it in data["items"] if str(it.get("id")) != str(group_id)]
    if len(data["items"]) == before:
        return jsonify({"error": "not found"}), 404
    data["updated_at"] = _now()
    with _lock_for("bookmark_groups"):
        write_json("bookmark_groups", data)
    # 回收引用该分组的书签，回退为未分组
    bdata = read_json("bookmarks", {"items": []})
    changed = False
    for it in bdata["items"]:
        if str(it.get("group") or "") == str(group_id):
            it["group"] = None
            changed = True
    if changed:
        bdata["updated_at"] = _now()
        with _lock_for("bookmarks"):
            write_json("bookmarks", bdata)
    return jsonify({"ok": True})

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
    """通用文件上传（如待办/笔记配图），只存文件、不落库，返回 stored_name。"""
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
# 腾讯学堂直播预告：由 cron 定时任务调用 QLearning MCP 抓取后写入，前端首页展示
# ---------------------------------------------------------------------------
@app.route("/api/qlearning-lives", methods=["GET"])
def get_qlearning_lives():
    data = read_json("qlearning_lives", {"items": []})
    return jsonify(data)

@app.route("/api/qlearning-lives/sync", methods=["POST"])
def sync_qlearning_lives():
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items", [])
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item = dict(it)
        if not item.get("id"):
            seed = str(item.get("href") or item.get("title") or uuid.uuid4().hex)
            item["id"] = uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:12]
        cleaned.append(item)
    cleaned.sort(key=lambda x: x.get("startTime") or "")
    with _lock_for("qlearning_lives"):
        write_json("qlearning_lives", {"items": cleaned, "updated_at": _now()})
    touch_meta()
    return jsonify({"ok": True, "count": len(cleaned)})

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
