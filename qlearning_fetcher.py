"""学堂直播抓取模块：直连 QLearning MCP，拉取当前用户有权限的直播回看列表。

协议：streamable-http + JSON-RPC 2.0，鉴权 Authorization: Bearer <TAI_IT_TOKEN>
（与 beacon_fetcher.py 同一枚太湖个人令牌）。

工具：getLiveReplayList —— 分页查询已结束、有回放的直播，返回
structuredContent.courses（含 contentName / contentUrl / startTime / endTime /
labels / photoUrl / playTotalCount 等）。

用法：
    fetch_lives(size=60)  -> [{id, href, title, startTime, endTime, tags, author, cover, views, source}]
"""
import datetime
import json
import os
import urllib.request

BASE = "https://qlearning.mcp.it.woa.com/api/mcp"
TZ8 = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_SIZE = 60


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


def _parse(body):
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body)


def _call_tool(name, arguments, session):
    _, body = _post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, session)
    rpc = _parse(body)
    if rpc.get("error"):
        raise RuntimeError(str(rpc["error"])[:300])
    result = rpc.get("result", {})
    if result.get("isError"):
        raise RuntimeError("QLearning tool error: " + str(result.get("content"))[:300])
    return result


class _QLSession:
    def __init__(self):
        self.sid = None

    def ensure(self):
        if self.sid:
            return
        self.sid, _ = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "worktable", "version": "1.0.0"}},
        })
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.sid)


def _normalize(course):
    href = course.get("contentUrl") or ""
    item_id = str(course.get("itemId") or "")
    # 优先用直播链接里的 liveId 作为 id，与现有数据格式保持一致
    live_id = ""
    if "liveId=" in href:
        live_id = href.split("liveId=", 1)[1].split("&", 1)[0]
    stable_id = live_id[:12] or __import__("hashlib").md5(href.encode()).hexdigest()[:12]
    return {
        "id": stable_id,
        "href": href,
        "title": course.get("contentName") or "",
        "author": "",
        "startTime": course.get("startTime") or "",
        "endTime": course.get("endTime") or "",
        "tags": course.get("labels") or [],
        "cover": course.get("photoUrl") or "",
        "views": course.get("playTotalCount"),
        "source": "qlearning",
    }


def fetch_lives(size=DEFAULT_SIZE, keyword=""):
    """拉取直播回看列表，映射为工作台 qlearning_lives 的 item 结构。"""
    if not get_token():
        raise RuntimeError("未配置 TAI_IT_TOKEN，无法抓取学堂直播")
    session = _QLSession()
    session.ensure()
    args = {"page": 1, "size": min(max(int(size), 1), 100), "orderBy": "time"}
    if keyword:
        args["keyword"] = keyword
    result = _call_tool("getLiveReplayList", args, session.sid)
    structured = result.get("structuredContent") or {}
    courses = structured.get("courses") or []
    return [_normalize(c) for c in courses]


if __name__ == "__main__":
    lives = fetch_lives()
    print(f"抓到 {len(lives)} 条直播回看：")
    for item in lives[:5]:
        print("-", item["startTime"], item["title"][:50], item["href"])
