"""with 分支专用：把操作类数据的读写转发到云端轻量服务。

云端是唯一的主库和抓取方；本服务只保存工作总结与内网 Agent 推送的数据。
启用方式：在 .env 里配置 CLOUD_BASE_URL 与 CLOUD_API_TOKEN。
不带这两个变量时（例如云端分支）本模块完全不生效。
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

_BASE = (os.environ.get("CLOUD_BASE_URL", "") or "").rstrip("/")
_TOKEN = (os.environ.get("CLOUD_API_TOKEN", "") or "").strip()

# 操作类数据：读写都转发到云端。
# 这些资源在云端使用同一套 /api/<name> CRUD 接口。
PROXY_NAMES = {
    "tasks", "notes", "bookmarks", "bookmark_groups", "knowledge",
    "products", "inbox", "media", "creators",
    }

# 云端抓取的数据：本服务不抓取，读取时转发云端接口。
# 值是与 HTTP 接口名不同的资源名映射；None 表示接口名与资源名相同。
CLOUD_READ_ONLY = {
    "news": None,
    "ai_pulse": None,
    "aihot_weekly": "ai_pulse/weekly",
    "douban_hot": "media/douban-hot",
    "imate_ops": None,
    "product_insights": None,  # 已下线，云端如无数据会返回空
}


def enabled():
    return bool(_BASE and _TOKEN)


def should_proxy(name):
    return enabled() and name in PROXY_NAMES


def should_cloud_read(name):
    return enabled() and name in CLOUD_READ_ONLY


def _request(path, method="GET", body=None, timeout=15):
    url = _BASE + path
    data = None
    headers = {
        "Authorization": "Bearer " + _TOKEN,
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", errors="ignore"))


def cloud_read(name, default=None):
    """从云端读取资源；返回 (ok, data)。失败时返回 (False, default)。"""
    path_name = CLOUD_READ_ONLY.get(name)
    if path_name is None:
        path_name = name
    try:
        data = _request("/api/" + path_name)
        return True, data if data is not None else default
    except Exception as exc:
        print(f"[cloud_proxy] 读取 {name} 失败: {exc}", flush=True)
        return False, default


def cloud_list(name, default=None):
    try:
        data = _request("/api/" + name)
        return True, data if data is not None else default
    except Exception as exc:
        print(f"[cloud_proxy] 读取 {name} 失败: {exc}", flush=True)
        return False, default


def cloud_write(name, data, method="POST", path=None):
    """把整份资源写回云端：按 id 做差异同步（新增 POST / 更新 PATCH / 删除 DELETE）。

    云端只提供单条 CRUD，没有整份覆盖接口，所以这里不直接 POST 整份文档，
    否则会把整个文档当成一条 item 塞进 items 数组。
    """
    target = path or ("/api/" + name)
    items = (data or {}).get("items")
    if not isinstance(items, list):
        try:
            _request(target, method=method, body=data)
            return True
        except Exception as exc:
            print(f"[cloud_proxy] 写入 {name} 失败: {exc}", flush=True)
            return False
    try:
        remote = _request(target) or {"items": []}
        remote_items = remote.get("items", []) if isinstance(remote, dict) else []
        local_ids = {str(it.get("id")) for it in items if it.get("id")}
        remote_by_id = {str(it.get("id")): it for it in remote_items if it.get("id")}
        for item in items:
            iid = str(item.get("id") or "")
            if iid and iid in remote_by_id:
                if item != remote_by_id[iid]:
                    _request(f"{target}/{iid}", method="PATCH", body=item)
            else:
                _request(target, method="POST", body=item)
        for iid in remote_by_id:
            if iid not in local_ids:
                _request(f"{target}/{iid}", method="DELETE")
        return True
    except Exception as exc:
        print(f"[cloud_proxy] 写入 {name} 失败: {exc}", flush=True)
        return False
