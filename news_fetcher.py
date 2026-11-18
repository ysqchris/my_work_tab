#!/usr/bin/env python3
"""新闻动态本地抓取模块（工作台自带，不依赖外部 Agent）。

数据源：
  - AIhot (https://aihot.virxact.com)  公开的「精选(selected)」AI 资讯，取最关键 20 条
  - GitHub Trending (https://github.com/trending)  取最热的前 5 个仓库

输出：与 news.json 结构一致的 items 列表，字段对齐前端展示：
  title / url / date / desc / source / topic / id / created_at
"""
import json
import re
import html
import datetime
import time
import subprocess
import urllib.request
import urllib.error
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin

# 可选翻译后端：deep-translator（Google 翻译），缺失时回退本地词典摘要
_translator = None
try:
    from deep_translator import GoogleTranslator  # 轻量，仅依赖 requests
    _translator = GoogleTranslator(source="en", target="zh-CN")
except Exception:
    _translator = None


def _load_dotenv():
    """独立运行时也能读到 .env（经 app.py 加载时环境变量已就绪，此处仅补缺）。"""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and not os.environ.get(k, "").strip():
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

# LLM 翻译后端（DeepSeek，OpenAI 兼容协议）。内网直连 Google 翻译常年失败，
# 故以 LLM 为主力翻译通道：批量成句翻译，质量远好于离线词典的碎片拼接。
import os as _os
_AI_API_KEY = (_os.environ.get("AI_API_KEY", "") or _os.environ.get("HYUNYUAN_API_KEY", "")).strip()
_AI_BASE_URL = (_os.environ.get("AI_BASE_URL", "") or _os.environ.get("HYUNYUAN_BASE_URL", "")
                or "https://api.deepseek.com").rstrip("/")
_AI_MODEL = (_os.environ.get("AI_MODEL", "") or _os.environ.get("HYUNYUAN_MODEL", "") or "deepseek-chat")

# 翻译结果进程内缓存：原文 -> 中文，避免同一标题在多轮抓取中重复调用
_TRANS_CACHE = {}
_LLM_BATCH_SIZE = 20  # 单次请求最多翻译多少条，过大易触发截断

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_AIHOT_UA = "Mozilla/5.0 (compatible; aihot-skill/0.2.0)"

# 各源的专题（topic）与来源名（source）
AIHOT_TOPIC = "AI热点"
AIHOT_SOURCE = "AI HOT"
GH_TOPIC = "GitHub 热榜"
GH_SOURCE = "GitHub Trending"

_AIHOT_ITEMS = "https://aihot.virxact.com/api/public/items"
_GH_TRENDING = "https://github.com/trending"

_AIHOT_LIMIT = 40
_GH_LIMIT = 10

# 覆盖模型发布/评测、产品发布/案例、行业人物分享，而非仅偏向办公Agent。
_NEWS_SEARCH_KEYWORDS = [
    "新模型", "模型评测", "benchmark", "GPT", "Claude", "Gemini", "DeepSeek", "Qwen",
    "新产品", "产品发布", "产品案例", "AI应用", "融资", "访谈", "演讲", "Karpathy", "黄仁勋",
]

_MODEL_EVENT_RE = re.compile(
    r"新模型|模型.{0,12}(发布|上线|开源|更新|评测|测评|评估)|"
    r"(发布|推出|开源|评测|测评|评估).{0,16}(模型|大模型)|"
    r"benchmark|leaderboard|模型榜单|模型对比|模型能力|model\s*(release|evaluation|benchmark)|"
    r"(?:GPT|Claude|Gemini|DeepSeek|Qwen|Llama|GLM|Kimi|Veo|Sora)[\s-]*\d[\w. -]*",
    re.I,
)
_PRODUCT_EVENT_RE = re.compile(
    r"产品|应用|客户|案例|办公|浏览器|插件|访谈|演讲|分享|融资|收购|合作|"
    r"customer|case\s*study|interview|podcast|partnership|funding|browser|workspace", re.I,
)


def classify_news_item(item):
    """统一分类：GitHub来源独占趋势版，模型发布评测优先，其余归行业版。"""
    if item.get("source") == GH_SOURCE or item.get("topic") == GH_TOPIC:
        return "github"
    title = " ".join(str(item.get(k) or "") for k in ("title", "title_en"))
    category = str(item.get("category") or "").lower()
    if _MODEL_EVENT_RE.search(title):
        return "models"
    if _PRODUCT_EVENT_RE.search(title):
        return "news"
    if category in {"ai-models", "model", "models", "model-review", "benchmark", "evaluation"}:
        return "models"
    # 泛论文/tech标签不等于模型更新或GitHub趋势，避免误分类。
    return "news"


# Agent 协作重点产品关键词（AIhot 扩展搜索，逐个 q= 抓取补充）
_AGENT_PRODUCT_KEYWORDS = [
    "豆包工作", "豆包工作伙伴", "豆包", "千问办公", "阿里办公", "通义千问",
    "飞书", "钉钉", "智能体", "多智能体", "AI 办公", "Copilot", "Manus", "OpenClaw",
]

# Agent 协作方向判定：仅匹配「Agent 与 Agent 之间的团队协作 / 多智能体编排」语义。
# 注意：单个 Agent、普通 AI 助手、RAG、Copilot 代码补全等不算「Agent 协作」，
# 因此这里不收录 agent / assistant / copilot / cursor / productivity 等泛化词。
_AGENT_TOPIC_RE = re.compile(
    r"multi-?agent|multiagent|多智能体|多\s*agent|多智能体协作|agent\s*(team|collaborat|orchestrat|swarm|crew|society)|"
    r"agent\s*(协作|团队|编排|集群)|智能体\s*(协作|团队|编排|集群|协同)|"
    r"agentic\s*(workflow|orchestrat|team|swarm|pipeline)|orchestrat\w*\s*(agent|multi)|"
    r"agent\s*(handoff|delegat|message\s*bus|communication\s*protocol)|"
    r"subagent|sub-?agent|swarm\s*intellig|agent\s*society|agent\s*mesh|"
    r"crewai|autogen|langgraph|agno|mastra|openhands|agent\.md|agora|agentprotocol|"
    r"agent2agent|a2a\s*protocol|mcp\s*(server|client)|acp\s*protocol|"
    r"团队?协作?\s*智能体|智能体\s*工作流|多agent\s*框架",
    re.I,
)


def _now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


def _today():
    return _now()[:10]


def _get(url, timeout=15, ua=None):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua or _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        enc = resp.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(enc, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")
    except Exception:
        # urllib 对部分站点（如 deepmind.google）存在 TLS 协商失败，回退 curl 抓取
        return _get_via_curl(url, timeout=timeout, ua=ua)


def _get_via_curl(url, timeout=15, ua=None):
    """用系统 curl 抓取，解决 urllib 的 SSL/TLS 兼容问题。"""
    cmd = ["curl", "-sL", "--max-time", str(timeout),
           "-A", ua or _UA, "--compressed", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except Exception:
        return ""
    raw = out.stdout
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _post_json(url, body=None, timeout=15, ua=None):
    """POST JSON 请求，返回解码后的文本。用于 36kr 等需要 POST 的接口。"""
    payload = dict(body or {})
    if "timestamp" in payload and payload["timestamp"] == 0:
        payload["timestamp"] = int(time.time() * 1000)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": ua or _UA,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    enc = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return s.strip()


# 离线兜底用的英中技术术语词典（deep-translator 不可用时才启用）
_TERM_DICT = {
    "framework": "框架", "library": "库", "tool": "工具", "toolkit": "工具包",
    "utility": "实用工具", "utilities": "实用工具集", "package": "包", "module": "模块",
    "plugin": "插件", "extension": "扩展", "client": "客户端", "server": "服务端",
    "cli": "命令行工具", "sdk": "SDK（软件开发工具包）", "api": "API", "ui": "界面",
    "gui": "图形界面", "engine": "引擎", "compiler": "编译器", "interpreter": "解释器",
    "runtime": "运行时", "platform": "平台", "application": "应用", "app": "应用",
    "apps": "应用", "system": "系统", "infrastructure": "基础设施", "service": "服务",
    "services": "服务", "daemon": "守护进程", "script": "脚本", "scripts": "脚本",
    "wrapper": "封装层", "bridge": "桥接工具", "boilerplate": "模板", "template": "模板",
    "starter": "起步模板", "generator": "生成器", "scaffold": "脚手架", "manager": "管理器",
    "llm": "大语言模型", "llms": "大语言模型", "ai": "AI", "agent": "智能体",
    "agents": "智能体", "rag": "RAG（检索增强生成）", "gpt": "GPT", "model": "模型",
    "models": "模型", "neural": "神经网络", "diffusion": "扩散模型", "embedding": "嵌入",
    "embeddings": "嵌入向量", "prompt": "提示词", "fine-tuning": "微调", "finetune": "微调",
    "inference": "推理", "training": "训练", "chatbot": "聊天机器人", "assistant": "助手",
    "vision": "视觉", "speech": "语音", "multimodal": "多模态", "build": "构建",
    "building": "构建", "create": "创建", "creating": "创建", "manage": "管理",
    "managing": "管理", "query": "查询", "understand": "理解", "edit": "编辑",
    "generate": "生成", "generating": "生成", "automate": "自动化", "automated": "自动化",
    "deploy": "部署", "deployment": "部署", "monitor": "监控", "analyze": "分析",
    "analysis": "分析", "analyse": "分析", "visualize": "可视化", "visualization": "可视化",
    "render": "渲染", "parse": "解析", "scrape": "抓取", "scraping": "抓取",
    "optimize": "优化", "optimization": "优化", "schedule": "定时", "scheduling": "定时",
    "notification": "通知", "notifications": "通知", "dashboard": "看板",
    "production-grade": "生产级", "production": "生产环境", "lightweight": "轻量",
    "simple": "简单", "fast": "快速", "minimal": "极简", "modern": "现代化",
    "self-hosted": "可自托管", "open-source": "开源", "opensource": "开源",
    "cross-platform": "跨平台", "real-time": "实时", "realtime": "实时", "multi": "多",
    "multilingual": "多语言", "scalable": "可扩展", "flexible": "灵活", "powerful": "强大",
    "web": "Web", "website": "网站", "frontend": "前端", "front-end": "前端",
    "backend": "后端", "back-end": "后端", "full-stack": "全栈", "fullstack": "全栈",
    "database": "数据库", "cache": "缓存", "queue": "队列", "graph": "图谱",
    "knowledge graph": "知识图谱", "blockchain": "区块链", "crypto": "加密货币",
    "security": "安全", "authentication": "认证", "encryption": "加密", "react": "React",
    "vue": "Vue", "node": "Node", "node.js": "Node.js", "python": "Python",
    "javascript": "JavaScript", "typescript": "TypeScript", "rust": "Rust", "golang": "Go",
    "go": "Go", "bash": "Bash", "shell": "Shell", "docker": "Docker",
    "kubernetes": "Kubernetes", "linux": "Linux", "posix-compliant": "符合 POSIX 标准",
    "explanatory": "讲解用", "math": "数学", "animation": "动画", "animations": "动画",
    "videos": "视频", "stock": "股票", "market": "市场", "markets": "市场",
    "codebase": "代码库", "codebases": "代码库", "monorepo": "单体仓库",
    "repository": "仓库", "version": "版本", "context": "上下文", "accountable": "可问责",
    "native": "原生", "community": "社区", "wizards": "高手", "ninjas": "高手",
    "personality": "个性", "processes": "流程", "deliverables": "交付物",
    "proven": "经过验证", "complete": "完整", "ultimate": "终极",
    "at your fingertips": "触手可及", "powered": "驱动", "driven": "驱动", "based": "基于",
}
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _gen_id(seed):
    return uuid.uuid5(uuid.NAMESPACE_URL, str(seed)).hex[:12]


def _has_cjk(s):
    return bool(_CJK.search(s or ""))


def _translate_dict(en_text):
    """离线词典兜底翻译：仅命中术语表的部分会被保留，未命中片段丢弃。"""
    text = " " + (en_text or "").lower() + " "
    for term, zh in sorted(_TERM_DICT.items(), key=lambda x: -len(x[0])):
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
        text = pat.sub("\u0001" + zh + "\u0001", text)
    out = []
    for p in re.split("\u0001", text):
        p = p.strip()
        if p and _has_cjk(p):
            out.append(p)
    return "".join(out)


def _llm_translate_batch(texts):
    """用 LLM 批量把英文翻译成中文，返回 {原文: 中文}。失败返回空字典。

    批量成句翻译的好处：一次请求覆盖多条，既省调用又能让模型参考上下文，
    且产品名/版本号可通过提示词要求保留不译。
    """
    if not _AI_API_KEY or not texts:
        return {}
    out = {}
    uniq = []
    for t in texts:
        t = (t or "").strip()
        if t and t not in uniq:
            uniq.append(t)
    for i in range(0, len(uniq), _LLM_BATCH_SIZE):
        chunk = uniq[i:i + _LLM_BATCH_SIZE]
        numbered = "\n".join(f"{n + 1}. {t}" for n, t in enumerate(chunk))
        prompt = (
            "你是科技资讯译者。把下面每行英文翻译成简洁、通顺的中文，用于中文资讯列表展示。\n"
            "要求：\n"
            "1) 产品名、公司名、模型名与版本号保留原文不译"
            "（如 Claude / Gemini 3.5 Flash / GPT / OpenAI / GitHub）；\n"
            "2) 每条控制在 40 字内，不加书名号、引号或结尾句号；\n"
            "3) 只输出一个 JSON 数组，元素为字符串，顺序和数量与输入完全一致，不要任何解释文字。\n\n"
            + numbered
        )
        body = json.dumps({
            "model": _AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1600,
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(_AI_BASE_URL + "/chat/completions", data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _AI_API_KEY,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            content = payload["choices"][0]["message"]["content"].strip()
            # 模型可能用 ```json 包裹，先剥掉围栏再取数组
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
            m = re.search(r"\[[\s\S]*\]", content)
            arr = json.loads(m.group(0) if m else content)
            if not isinstance(arr, list):
                raise ValueError("返回不是数组")
            for src, zh in zip(chunk, arr):
                zh = re.sub(r"\s+", " ", str(zh or "")).strip()
                if zh and _has_cjk(zh):
                    out[src] = zh
        except Exception as e:
            print(f"[news_fetcher] LLM 批量翻译失败（{len(chunk)} 条）: {e}", flush=True)
    return out


def prewarm_translations(texts):
    """抓取完成后统一批量翻译并写入缓存，供 translate_to_zh 命中。"""
    pending = [t for t in texts
               if t and not _has_cjk(t) and t.strip() not in _TRANS_CACHE]
    if not pending:
        return 0
    got = _llm_translate_batch(pending)
    _TRANS_CACHE.update(got)
    if got:
        print(f"[news_fetcher] LLM 翻译完成 {len(got)}/{len(pending)} 条", flush=True)
    return len(got)


def translate_to_zh(text):
    """把英文文本翻译成中文。

    入参 text 可能为空或已含中文：
      - 空：返回空串
      - 已含中文：直接返回中文部分（去掉尾部英文赘余）
      - 纯英文：依次尝试 缓存 → LLM → Google 翻译 → 离线词典
    """
    if not text:
        return ""
    if _has_cjk(text):
        # 取从首个中文字符到最后一个中文字符之间的整段（含其间英文/标点）
        m = re.search(r"[\u4e00-\u9fff][\s\S]*[\u4e00-\u9fff]", text)
        return (m.group(0) if m else text.strip()).strip()
    key = text.strip()
    if key in _TRANS_CACHE:
        return _TRANS_CACHE[key]
    # 主力通道：LLM（内网可直连，质量好于词典拼接）
    got = _llm_translate_batch([key])
    if got.get(key):
        _TRANS_CACHE[key] = got[key]
        return got[key]
    # 次选：Google 翻译（内网多数环境不可达，作为额外兜底保留）
    if _translator is not None:
        try:
            zh = _translator.translate(text)
            if zh and "No translation was found" not in zh:
                # Google 偶尔对含专有名词的句子返回原文（视为未翻译），回退词典
                norm = lambda s: re.sub(r"\s+", "", (s or "")).lower()
                if norm(zh) != norm(text):
                    _TRANS_CACHE[key] = zh.strip()
                    return zh.strip()
        except Exception:
            pass
    dzh = _translate_dict(text)
    result = dzh or text.strip()
    _TRANS_CACHE[key] = result
    return result


def _to_local_date(iso_utc):
    """把 ISO UTC 时间转成东八区 YYYY-MM-DD。"""
    if not iso_utc:
        return _today()
    try:
        s = iso_utc.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return _today()


def _parse_aihot_item(it):
    """把 AIhot 单条原始数据规整为统一结构，并标记是否 Agent 协作相关。"""
    title = _strip_tags(it.get("title") or it.get("title_en") or "")
    url_ = it.get("permalink") or it.get("url") or ""
    desc = _strip_tags(it.get("summary") or "")
    if desc:
        desc = re.sub(r"\s+", " ", desc).strip()[:160]
    date = _to_local_date(it.get("publishedAt"))
    if not title or not url_:
        return None
    source = (it.get("source") or "").strip() or AIHOT_SOURCE
    category = (it.get("category") or "ai-products").strip()
    hay = (title + " " + desc + " " + source).lower()
    return {
        "title": title,
        "url": url_,
        "date": date,
        "desc": desc,
        "source": source,
        "topic": AIHOT_TOPIC,
        "category": category,
        "score": it.get("score"),
        "published_at": it.get("publishedAt") or "",
        "id": _gen_id(url_ or title),
        "created_at": _now(),
        "agent_related": bool(_AGENT_TOPIC_RE.search(hay)),
    }


def fetch_aihot(limit=_AIHOT_LIMIT):
    """抓取 AIhot 精选 AI 资讯，取最关键 limit 条。

    保留 aihot 原始分类（category）与来源（source），用于前端
    「AI 行业动态 / 模型更新 / GitHub 趋势 / 产品案例」四板块分组及来源徽章展示。
    """
    items = []
    try:
        url = f"{_AIHOT_ITEMS}?mode=selected&take={limit}"
        txt = _get(url, ua=_AIHOT_UA)
        data = json.loads(txt)
        raw_items = data.get("items", []) if isinstance(data, dict) else data
        for it in raw_items[:limit]:
            item = _parse_aihot_item(it)
            if item:
                items.append(item)
    except Exception as e:
        print(f"[news_fetcher] AIhot 抓取失败: {e}")
    return items


def fetch_aihot_keywords(keywords=_AGENT_PRODUCT_KEYWORDS, per_kw=5):
    """按 Agent 协作重点产品关键词搜索 AIhot，补充抓取相关资讯。

    AIhot 精选（selected）偏向行业大新闻，未必覆盖办公协作类产品，
    故额外用 q= 逐个关键词搜索，把这些产品动态「抓进来」，再与精选合并去重。
    """
    items = []
    seen = set()
    for kw in keywords:
        try:
            url = f"{_AIHOT_ITEMS}?q={quote(kw)}&take={per_kw}"
            txt = _get(url, ua=_AIHOT_UA)
            data = json.loads(txt)
            raw = data.get("items", []) if isinstance(data, dict) else data
            for it in raw[:per_kw]:
                item = _parse_aihot_item(it)
                if not item:
                    continue
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                items.append(item)
        except Exception as e:
            print(f"[news_fetcher] AIhot 关键词「{kw}」搜索失败: {e}")
    return items


def fetch_github_trending(limit=_GH_LIMIT):
    """抓取 GitHub Trending，取最热的前 limit 个仓库。

    除仓库名/链接外，还抓取：项目描述、主语言、当日新增 Star 数，
    并调用 translate_to_zh 生成一段中文摘要（desc 字段）。
    """
    items = []
    try:
        html_text = _get(_GH_TRENDING)
        blocks = re.findall(r"<article class=\"Box-row\">(.*?)</article>", html_text, re.S)
        for block in blocks[:limit]:
            m = re.search(r"<h2[^>]*>.*?<a[^>]*href=\"([^\"]+)\"", block, re.S)
            if not m:
                continue
            repo_path = m.group(1).strip()
            if repo_path.startswith("/"):
                repo_path = repo_path[1:]
            # 描述：Trending 现把描述放在仓库标题 <a> 之后的 <p>（class 含 col-9/my-1）中
            dm = re.search(r"<p[^>]*class=\"[^\"]*(col-9|my-1|color-fg-muted)[^\"]*\"[^>]*>(.*?)</p>",
                           block, re.S)
            desc_en = _strip_tags(dm.group(2)) if dm else ""
            # 主语言
            lm = re.search(r"<span itemprop=\"programmingLanguage\">([^<]+)</span>", block)
            lang = lm.group(1).strip() if lm else ""
            # Star 数：Trending 页只给出「当日新增」，历史总 Star 不展示，故仅取 stars_today
            sm_today = re.search(r"([\d,]+)\s*stars (this|today)", block, re.I)
            stars_total = None
            stars_today = int(sm_today.group(1).replace(",", "")) if sm_today else None

            url = "https://github.com/" + repo_path
            hay = (repo_path + " " + desc_en).lower()

            items.append({
                "title": repo_path,
                "url": url,
                "date": _today(),
                "desc": "",           # 中文摘要由 fetch_all 批量翻译后回填
                "desc_en": desc_en[:200],
                "lang": lang,
                "stars_total": stars_total,
                "stars_today": stars_today,
                "source": GH_SOURCE,
                "topic": GH_TOPIC,
                "category": "tech",
                "id": _gen_id(url),
                "created_at": _now(),
                "agent_related": bool(_AGENT_TOPIC_RE.search(hay)),
            })
    except Exception as e:
        print(f"[news_fetcher] GitHub Trending 抓取失败: {e}")
    return items


# 产品案例源：各产品官方博客，抓取「用法案例 · 推荐用法 · 落地实践」
# kind 决定解析方式：
#   rss       : 标准 RSS/Atom feed
#   claude    : HTML 卡片，data-cta-copy 标题 + /blog/ 链接
#   deepmind  : Google DeepMind 博客卡片，data-event-content-name 标题 + blog.google 链接
#   36kr      : 主站热榜 POST 接口，AI 关键词过滤
# 说明：版本发布 / changelog / release notes 类「产品更新」信息已移出，
#       归到「鹰眼」板块，此处不再抓取（WorkBuddy、Coze、CrewAI、Multica、ClickUp、Taskade 等）。
_PRODUCT_CASE_SOURCES = [
    {"name": "OpenAI", "kind": "rss", "url": "https://openai.com/news/rss.xml"},
    {"name": "Claude", "kind": "claude", "url": "https://claude.com/blog"},
    {"name": "Gemini", "kind": "deepmind", "url": "https://deepmind.google/blog/"},
    {"name": "36氪", "kind": "36kr", "method": "post",
     "url": "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot",
     "body": {"partner_id": "web", "timestamp": 0,
              "param": {"siteId": 1, "platformId": 2}}},
]

_NAV_TITLE_RE = re.compile(
    r"^(skip to content|skip to main content|get started|login|sign up|sign in|"
    r"view all|view release|read more|learn more|next|previous|back to top|"
    r"features|pricing|docs|documentation|blog|changelog|github|twitter|x|home|首页|"
    r"联系我们|关于我们|隐私|条款|安装|入门指南|browse feeds|latest|create account|"
    r"releasebot|sign up for free|dashboard|use cases|start cloud trial|home page)$",
    re.I,
)

# 36kr 主站热榜过滤：只保留 AI 相关资讯的关键词
_AI_CASE_RE = re.compile(
    r"AI|人工智能|智能体|Agent|大模型|多模态|生成式|AIGC|机器学习|深度学习|"
    r"ChatGPT|GPT|Claude|Gemini|OpenAI|Anthropic|DeepSeek|通义|文心|豆包|"
    r"Copilot|Cursor|MCP|LLM|机器人|自动驾驶|具身智能|提示词|神经网络",
    re.I,
)


def _entry(title, url, date=""):
    """构造统一的产品案例条目。"""
    title = re.sub(r"\s+", " ", (title or "")).strip()
    if not title or len(title) < 6 or len(title) > 160:
        return None
    if _NAV_TITLE_RE.match(title):
        return None
    url = (url or "").strip()
    if not url.startswith("http"):
        return None
    hay = title.lower()
    return {
        "title": title,
        "url": url,
        "date": date or _today(),
        "desc": "",
        "source": "",
        "topic": "产品案例",
        "category": "product-case",
        "score": None,
        "published_at": "",
        "id": _gen_id(url or title),
        "created_at": _now(),
        "agent_related": bool(_AGENT_TOPIC_RE.search(hay)),
    }


def _parse_workbuddy(txt, base, limit):
    out = []
    for m in re.finditer(r'<a[^>]+href="#([^"]+)"[^>]*aria-label="Permalink to &quot;(.*?)&quot;"', txt):
        anchor, label = m.group(1), html.unescape(m.group(2)).strip()
        if "版本发布" not in label and not re.search(r"\d+\.\d+", label):
            continue
        title = re.sub(r"\s*🚀\s*", " ", label).strip()
        date = ""
        dm = re.search(r"（(\d{4}-\d{2}-\d{2})）", label)
        if dm:
            date = dm.group(1)
        e = _entry(title, urljoin(base, "#" + anchor), date)
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_coze(txt, base, limit):
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', txt, re.S):
        href, title = m.group(1), re.sub(r"\s+", " ", _strip_tags(m.group(2))).strip()
        if re.search(r"访问控制|身份验证|批量切换|技能环境变量|集成|空间资源", title):
            continue
        url = urljoin(base, href)
        if "docs.coze.cn" not in url:
            continue
        e = _entry(title, url)
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_crewai(txt, base, limit):
    out = []
    seen = set()
    for m in re.finditer(r'releases/tag/([0-9.]+)"', txt):
        ver = m.group(1)
        if ver in seen:
            continue
        seen.add(ver)
        e = _entry(f"CrewAI v{ver} 发布", f"https://github.com/crewAIInc/crewAI/releases/tag/{ver}")
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_multica(txt, base, limit):
    out = []
    anchors = re.findall(r'release-0-4-(\d+)', txt)
    titles = [re.sub(r"\s+", " ", _strip_tags(t)).strip()
              for t in re.findall(r'<h2[^>]*>\s*(.*?)\s*</h2>', txt, re.S)]
    titles = [t for t in titles if t and t.lower() not in ("all releases", "changelog")]
    for i, ver in enumerate(anchors[:limit]):
        title = titles[i] if i < len(titles) else f"Multica v0.4.{ver} 发布"
        e = _entry(title, f"https://multica.ai/changelog#release-0-4-{ver}")
        if e:
            out.append(e)
    return out[:limit]


def _parse_clickup(txt, base, limit):
    out = []
    for m in re.finditer(r'<h3[^>]*>\s*(.*?)\s*</h3>', txt, re.S):
        title = re.sub(r"\s+", " ", _strip_tags(m.group(1))).strip()
        e = _entry(title, base)
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_rss(txt, base, limit):
    from email.utils import parsedate_to_datetime
    out = []
    try:
        root = ET.fromstring(txt)
    except Exception:
        return out
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: c for c in item}
        def text(key):
            node = fields.get(key)
            return "" if node is None else "".join(node.itertext()).strip()
        link = text("link")
        for node in item:
            if node.tag.rsplit("}", 1)[-1] == "link" and node.get("href") and node.get("rel", "alternate") == "alternate":
                link = node.get("href")
                break
        e = _entry(text("title"), urljoin(base, link) if link else "")
        if e:
            pub = text("pubDate") or text("published") or text("updated")
            try:
                dt = parsedate_to_datetime(pub) if text("pubDate") else datetime.datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                e["date"] = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                e["published_at"] = dt.isoformat()
            except (ValueError, TypeError, OverflowError):
                e["date"] = ""
            e["desc"] = re.sub(r"\s+", " ", _strip_tags(text("description") or text("summary") or text("content")))[:500]
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_claude(txt, base, limit):
    out = []
    seen = set()
    # Claude 博客卡片：<a data-cta-copy="标题" ... href="/blog/xxx">，标题与链接配对
    for m in re.finditer(r'<a\b[^>]*data-cta-copy="([^"]*)"[^>]*href="(/blog/[^"]+)"', txt):
        title = html.unescape(m.group(1)).strip()
        href = m.group(2)
        if href in seen:
            continue
        seen.add(href)
        e = _entry(title, urljoin(base, href))
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_36kr(txt, base, limit):
    out = []
    try:
        data = json.loads(txt)
    except Exception:
        return out
    lst = (data.get("data") or {}).get("hotRankList") or []
    for it in lst:
        tm = it.get("templateMaterial") or {}
        title = (tm.get("widgetTitle") or "").strip()
        item_id = it.get("itemId")
        if not title or not item_id:
            continue
        # 只保留 AI 相关资讯（用户要的是 AI 产品案例，主站热榜会混入财经/数码等）
        if not _AI_CASE_RE.search(title):
            continue
        url = f"https://36kr.com/p/{item_id}"
        e = _entry(title, url)
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def _parse_deepmind(txt, base, limit):
    """Google DeepMind 博客：卡片标题通过 data-event-content-name="标题 - Learn more" 与链接配对。"""
    out = []
    seen = set()
    for m in re.finditer(
        r'data-event-content-name="([^"]*?)"[^>]*href="(https://blog\.google[^"]+)"', txt
    ):
        name = html.unescape(m.group(1)).strip()
        href = m.group(2).split("?")[0]
        if not name.endswith("Learn more"):
            continue
        title = name[:-len("Learn more")].rstrip("- ").strip()
        if not title or href in seen:
            continue
        seen.add(href)
        e = _entry(title, href)
        if e:
            out.append(e)
        if len(out) >= limit:
            break
    return out


_PARSERS = {
    "workbuddy": _parse_workbuddy,
    "coze": _parse_coze,
    "crewai": _parse_crewai,
    "multica": _parse_multica,
    "clickup": _parse_clickup,
    "rss": _parse_rss,
    "claude": _parse_claude,
    "36kr": _parse_36kr,
    "deepmind": _parse_deepmind,
}


def fetch_product_cases(per_source=4):
    """抓取各产品官方博客的用法案例，标记 category=product-case。

    英文标题不在此处逐条翻译（那样每条一次请求，慢且易触发限流），
    统一交由 fetch_all 批量翻译。
    """
    items = []
    for src in _PRODUCT_CASE_SOURCES:
        try:
            if src.get("method") == "post":
                txt = _post_json(src["url"], src.get("body"), ua=_UA)
            else:
                txt = _get(src["url"], ua=_UA)
            parser = _PARSERS.get(src["kind"], _parse_coze)
            for e in parser(txt, src["url"], per_source):
                e["source"] = src["name"]
                items.append(e)
        except Exception as ex:
            print(f"[news_fetcher] 产品案例源「{src['name']}」抓取失败: {ex}")
    return items


def _localize_titles(items):
    """把英文标题批量译成中文：title 换成中文，原文存入 title_en。

    只处理不含中文的标题；翻译失败的条目保持原样，不会丢内容。
    """
    targets = [it for it in items if not _has_cjk(it.get("title") or "")]
    if not targets:
        return
    prewarm_translations([it["title"] for it in targets])
    for it in targets:
        en = it["title"].strip()
        zh = _TRANS_CACHE.get(en)
        if zh and _has_cjk(zh) and zh != en:
            it["title_en"] = en          # 原文留存，前端可做 hover 对照
            it["title"] = zh
            # desc 若为空或与译文重复，则不再冗余展示
            if not (it.get("desc") or "").strip():
                it["desc"] = ""


def fetch_all():
    """汇总所有源，去重后返回 items（Agent 协作相关优先，其余按时间倒序）。"""
    from concurrent.futures import ThreadPoolExecutor
    sources = [
        lambda: fetch_aihot(limit=_AIHOT_LIMIT),
        lambda: fetch_aihot_keywords(keywords=_NEWS_SEARCH_KEYWORDS, per_kw=4),
        lambda: fetch_github_trending(limit=_GH_LIMIT),
        lambda: fetch_product_cases(per_source=6),
    ]
    items = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch in pool.map(lambda fetch: fetch(), sources):
            items.extend(batch)
    for item in items:
        item["board"] = classify_news_item(item)
    # 去重：同 url 或同 title 只保留第一条
    seen = set()
    cleaned = []
    for it in items:
        key = it.get("url") or it.get("title")
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(it)
    # 时间倒序（date 新在前）
    cleaned.sort(key=lambda x: (x.get("date") or "", x.get("created_at") or ""), reverse=True)
    # 稳定排序：Agent 协作相关的整体前置
    cleaned.sort(key=lambda x: 0 if x.get("agent_related") else 1)

    # —— 统一本地化：一次批量翻译覆盖全部英文内容 ——
    # GitHub 条目的 title 是仓库名（owner/repo），属专有名词不译，只译其描述。
    gh = [it for it in cleaned if it.get("topic") == GH_TOPIC]
    others = [it for it in cleaned if it.get("topic") != GH_TOPIC]
    try:
        prewarm_translations([it.get("desc_en") or "" for it in gh])
        for it in gh:
            en = (it.get("desc_en") or "").strip()
            if en:
                it["desc"] = (_TRANS_CACHE.get(en) or translate_to_zh(en))[:160]
        _localize_titles(others)
    except Exception as e:
        print(f"[news_fetcher] 批量翻译异常，保留原文: {e}", flush=True)
    return cleaned


if __name__ == "__main__":
    import sys
    data = fetch_all()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n共抓取 {len(data)} 条（AIhot {_AIHOT_LIMIT} + GitHub {_GH_LIMIT}）", file=sys.stderr)
