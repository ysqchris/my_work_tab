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

# AI 打分：OpenAI 兼容 chat/completions（复用工作台现有 DeepSeek 配置）
AI_API_KEY = ""
AI_BASE_URL = ""
AI_MODEL = ""


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

AI_API_KEY = (os.environ.get("AI_API_KEY", "") or "").strip()
AI_BASE_URL = (os.environ.get("AI_BASE_URL", "") or "https://api.deepseek.com").rstrip("/")
AI_MODEL = (os.environ.get("AI_MODEL", "") or "deepseek-chat").strip()


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


def fetch_raw_hot(period=2, limit=30):
    """抓取 KM 热榜原始候选（不做筛选）。"""
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
        articles.append({
            "id": _article_id(url),
            "title": title,
            "author": f.get("作者", ""),
            "date": f.get("创建时间", "")[:10],
            "created_at": f.get("创建时间", ""),
            "updated_at": f.get("更新时间", ""),
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
            "summary": f.get("导语", ""),
        })
    return articles


# AI 打分规则：来自用户明确的 KM 好文筛选偏好（2026-11-27）。
AI_SCORE_SYSTEM = """你在替一位做 imate / MA 数字同事（Agent 协作产品）的用户，从 KM 每日文章噪音里筛出值得读的几篇。
你不是搬运热榜，是私人信息过滤器。严格按下面三档口味判断：

【必选核心】Agent 协作方向的产品与落地案例（最优先）
- 命中主题：Agent / 智能体 / Multi-Agent / 数字同事 / MCP / AI 工作流 / Agent 工程 / 大模型应用
- 加分项：实战案例（有落地场景、有数据效果，比如"告警 Agent 体系"这类）；KM 头条 / KM 推荐 / 高阅读高收藏
【顺带保留】AI 新技术、新产品分享
- 多模态、推理模型、编程助手、AI 产品新形态等
- 但门槛是"硬核且浅显易懂"：有场景、有例子、有结论、讲人话；满屏术语公式源码的直接不要
【一律排除】
- 纯新闻资讯、活动通知、有奖征集、招聘
- 纯产品宣传公告（没干货的那种）
- 纯论文/学术推导、读起来吃力的

输出 JSON 数组，每个候选都要有一项（包括要排除的），字段：
  id: 候选项里给的 id（原样返回，不要改）
  keep: 是否值得保留（true/false）
  score: 0-100 的整数。100=今日最值得读（核心主题+实战案例/对标价值）；90+核心主题+头条推荐；80-89 核心主题或顺带保留里的优质项；70-79 顺带保留且浅显；<70 或排除项给低分
  priority: 是否重点推荐（布尔，通常 score>=90 才为 true）
  reason: 一句话中文理由，说明为什么保留或排除（排除也要写清原因）
只输出 JSON 数组，不要 Markdown 代码块，不要其它文字。"""


def _call_ai(messages, max_tokens=3000, timeout=90):
    """调用 OpenAI 兼容 chat/completions；失败抛异常。"""
    if not AI_API_KEY:
        raise RuntimeError("AI_API_KEY 未配置")
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        AI_BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + AI_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    return data["choices"][0]["message"]["content"]


def _ai_score(articles):
    """用 AI 给候选打分，返回 (保留的文章列表, 打分明细)。失败时降级全量保留。"""
    if not articles:
        return [], {}
    if not AI_API_KEY:
        print("[km] 未配置 AI_API_KEY，跳过 AI 打分，保留原始热榜", flush=True)
        return articles, {}
    candidates = [{
        "id": a["id"], "title": a["title"], "summary": a.get("summary", "")[:200],
        "tags": a.get("tags", []), "headline": a.get("headline", False),
        "recommended": a.get("recommended", False),
        "read_count": a.get("read_count", 0), "hot": a.get("hot", 0),
    } for a in articles]
    try:
        content = _call_ai([
            {"role": "system", "content": AI_SCORE_SYSTEM},
            {"role": "user", "content": json.dumps(candidates, ensure_ascii=False)},
        ])
        obj = json.loads(content)
        scores = obj.get("items", obj) if isinstance(obj, dict) else obj
        by_id = {str(s.get("id")): s for s in scores if isinstance(s, dict)}
    except Exception as e:
        print(f"[km] AI 打分失败，保留原始热榜: {e}", flush=True)
        return articles, {}

    picked = []
    for a in articles:
        s = by_id.get(str(a["id"]))
        if not s or not s.get("keep"):
            continue
        score = s.get("score")
        score = int(score) if isinstance(score, (int, float)) else 0
        reason = str(s.get("reason") or "").strip()
        a["recommendation"] = {
            "score": max(0, min(100, score)),
            "priority": bool(s.get("priority")),
            "reason": reason[:120],
        }
        picked.append(a)
    picked.sort(key=lambda x: -(x.get("recommendation", {}).get("score") or 0))
    return picked, {"total": len(articles), "kept": len(picked)}


def fetch_hot(period=2, limit=30, curate=True):
    """抓取热榜，默认用 AI 按用户口味精选并打分（curate=False 可关闭）。"""
    raw = fetch_raw_hot(period=period, limit=limit)
    if not curate:
        return raw
    picked, stat = _ai_score(raw)
    if stat:
        print(f"[km] AI 精选：{stat['total']} 篇候选中保留 {stat['kept']} 篇", flush=True)
    return picked


if __name__ == "__main__":
    import sys
    arts = fetch_hot(period=2, limit=10)
    print(json.dumps(arts, ensure_ascii=False, indent=2))
    print(f"共 {len(arts)} 篇", file=sys.stderr)
