"""KM 好文抓取模块：直连 KM MCP，拉取文章热榜 Top N。

协议：streamable-http + JSON-RPC 2.0，鉴权 Authorization: Bearer <TAI_IT_TOKEN>
（与 beacon_fetcher.py、qlearning_fetcher.py 同一枚太湖个人令牌）。

端点：https://km.mcp.woa.com
工具：hot-articles —— 获取文章热榜 Top N，参数 period(1/2/3/7)、limit。
返回 result.content[0].text，每篇文章一行的结构化文本。

用法：
    fetch_hot(period=2, limit=10) -> [{title, author, date, tags, url,
                                       read_count, like_count, fav_count,
                                       comment_count, hot, group, ...}]
"""
import datetime
import json
import os
import re
import urllib.request

BASE = "https://km.mcp.woa.com"
TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and not os.environ.get(k, "").strip():
                os.environ[k] = v


_load_env()


def get_token():
    return os.environ.get("TAI_IT_TOKEN", "").strip()


def _post(payload, session=None):
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + get_token(),
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(BASE, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        body = resp.read().decode("utf-8")
    return sid, body


def _parse_sse(body):
    """解析 streamable-http 的 text/event-stream 或纯 JSON 响应。"""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for block in body.split("\n\n"):
        data_line = next((ln[5:].strip() for ln in block.splitlines()
                          if ln.startswith("data:")), None)
        if data_line and data_line != "[DONE]":
            try:
                return json.loads(data_line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError("无法解析 MCP 响应: " + body[:200])


def _initialize():
    session, body = _post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "worktable-km-fetcher", "version": "1.0"},
        },
    })
    _parse_sse(body)
    return session


def _call_tool(name, arguments, session=None):
    payload = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    _, body = _post(payload, session=session)
    obj = _parse_sse(body)
    if "error" in obj:
        raise RuntimeError(f"KM MCP error: {obj['error']}")
    result = obj.get("result", {})
    texts = [c.get("text", "") for c in result.get("content", [])
             if c.get("type") == "text"]
    return "\n".join(texts)


def _split_fields(line):
    """按 ', 键名: ' 拆分单行字段；导语等含逗号需容错（用字段键名做分隔锚点）。"""
    keys = ["标题", "作者", "共同作者", "创建时间", "更新时间", "导语", "标签",
            "阅读", "点赞", "收藏", "评论", "KM推荐", "KM头条", "K吧", "热度", "链接"]
    pattern = r", (?=(?:%s): )" % "|".join(re.escape(k) for k in keys)
    parts = re.split(pattern, line.strip().lstrip("- ").strip())
    fields = {}
    for part in parts:
        if ": " in part:
            k, v = part.split(": ", 1)
            fields[k.strip()] = v.strip()
    return fields


def _to_int(value):
    try:
        return int(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _article_id(url):
    m = re.search(r"/show/(\d+)", url or "")
    if m:
        import hashlib
        return hashlib.md5(m.group(1).encode()).hexdigest()[:12]
    import hashlib
    return hashlib.md5((url or "").encode()).hexdigest()[:12]


def fetch_hot(period=2, limit=10):
    """返回热榜文章列表，字段与工作台 km_hot 数据存储结构兼容。"""
    if not get_token():
        print("[km] 未配置 TAI_IT_TOKEN，跳过抓取", flush=True)
        return []
    session = _initialize()
    text = _call_tool("hot-articles", {"period": period, "limit": limit},
                      session=session)
    articles = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-") and "标题:" not in line:
            continue
        f = _split_fields(line)
        title = f.get("标题", "")
        if not title:
            continue
        url = f.get("链接", "").split("?")[0]
        group = ""
        km = f.get("K吧", "")
        gm = re.match(r"#\d+\s+(.+?)\s+https?://", km)
        if gm:
            group = gm.group(1).strip()
        date = f.get("创建时间", "")[:10]
        articles.append({
            "id": _article_id(url),
            "title": title,
            "author": f.get("作者", ""),
            "date": date,
            "url": url,
            "tags": [t.strip() for t in f.get("标签", "").split("|") if t.strip()],
            "read_count": _to_int(f.get("阅读", "0")),
            "like_count": _to_int(f.get("点赞", "0")),
            "fav_count": _to_int(f.get("收藏", "0")),
            "comment_count": _to_int(f.get("评论", "0")),
            "hot": _to_int(f.get("热度", "0")),
            "group": group,
            "headline": f.get("KM头条", "") == "是",
            "recommended": f.get("KM推荐", "") == "是",
        })
    return articles


if __name__ == "__main__":
    import sys
    arts = fetch_hot(period=2, limit=10)
    print(json.dumps(arts, ensure_ascii=False, indent=2))
    print(f"共 {len(arts)} 篇", file=sys.stderr)
