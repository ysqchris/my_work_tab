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
import urllib.request
import urllib.error
import uuid

# 可选翻译后端：deep-translator（Google 翻译），缺失时回退本地词典摘要
_translator = None
try:
    from deep_translator import GoogleTranslator  # 轻量，仅依赖 requests
    _translator = GoogleTranslator(source="en", target="zh-CN")
except Exception:
    _translator = None

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

_AIHOT_LIMIT = 20
_GH_LIMIT = 5


def _now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


def _today():
    return _now()[:10]


def _get(url, timeout=15, ua=None):
    req = urllib.request.Request(url, headers={"User-Agent": ua or _UA})
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


def translate_to_zh(text):
    """把项目描述翻译成中文（优先 Google 翻译，失败回退离线词典）。

    入参 text 可能为空或已含中文：
      - 空：返回空串
      - 已含中文：直接返回中文部分（去掉尾部英文赘余）
      - 纯英文：调用翻译后端；异常时回退 _translate_dict
    """
    if not text:
        return ""
    if _has_cjk(text):
        # 取从首个中文字符到最后一个中文字符之间的整段（含其间英文/标点）
        m = re.search(r"[\u4e00-\u9fff][\s\S]*[\u4e00-\u9fff]", text)
        return (m.group(0) if m else text.strip()).strip()
    if _translator is not None:
        try:
            zh = _translator.translate(text)
            if zh and "No translation was found" not in zh:
                # Google 偶尔对含专有名词的句子返回原文（视为未翻译），回退词典
                norm = lambda s: re.sub(r"\s+", "", (s or "")).lower()
                if norm(zh) != norm(text):
                    return zh.strip()
            print(f"[news_fetcher] Google 翻译无效，回退词典: {text[:60]}")
        except Exception as e:
            print(f"[news_fetcher] Google 翻译失败，回退词典: {e}")
    dzh = _translate_dict(text)
    return dzh or text.strip()


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


def fetch_aihot(limit=_AIHOT_LIMIT):
    """抓取 AIhot 精选 AI 资讯，取最关键 limit 条。"""
    items = []
    try:
        url = f"{_AIHOT_ITEMS}?mode=selected&take={limit}"
        txt = _get(url, ua=_AIHOT_UA)
        data = json.loads(txt)
        raw_items = data.get("items", []) if isinstance(data, dict) else data
        for it in raw_items[:limit]:
            title = _strip_tags(it.get("title") or it.get("title_en") or "")
            # 优先用 aihot 站内 permalink（稳定、可点击），无则回退原始 url
            url_ = it.get("permalink") or it.get("url") or ""
            # desc：优先 summary，去掉多余空白
            desc = _strip_tags(it.get("summary") or "")
            if desc:
                desc = re.sub(r"\s+", " ", desc).strip()[:160]
            date = _to_local_date(it.get("publishedAt"))
            if not title or not url_:
                continue
            items.append({
                "title": title,
                "url": url_,
                "date": date,
                "desc": desc,
                "source": AIHOT_SOURCE,
                "topic": AIHOT_TOPIC,
                "id": _gen_id(url_ or title),
                "created_at": _now(),
            })
    except Exception as e:
        print(f"[news_fetcher] AIhot 抓取失败: {e}")
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
            # 中文摘要（优先 Google 翻译，已含中文则直用，失败回退离线词典）
            summary_zh = translate_to_zh(desc_en)

            items.append({
                "title": repo_path,
                "url": url,
                "date": _today(),
                "desc": summary_zh[:160],
                "desc_en": desc_en[:200],
                "lang": lang,
                "stars_total": stars_total,
                "stars_today": stars_today,
                "source": GH_SOURCE,
                "topic": GH_TOPIC,
                "id": _gen_id(url),
                "created_at": _now(),
            })
    except Exception as e:
        print(f"[news_fetcher] GitHub Trending 抓取失败: {e}")
    return items


def fetch_all():
    """汇总所有源，去重后返回 items（最新在前）。"""
    items = []
    items += fetch_aihot(limit=_AIHOT_LIMIT)      # 最关键 20 条 AI 资讯
    items += fetch_github_trending(limit=_GH_LIMIT)  # 最热前 5 仓库
    # 去重：同 url 或同 title 只保留第一条
    seen = set()
    cleaned = []
    for it in items:
        key = it.get("url") or it.get("title")
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(it)
    # 倒序：date 新的在前，其次 AI 资讯在前（保持专题分组感）
    cleaned.sort(key=lambda x: (x.get("date") or "", x.get("topic") or "", x.get("created_at") or ""),
                 reverse=True)
    return cleaned


if __name__ == "__main__":
    import sys
    data = fetch_all()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n共抓取 {len(data)} 条（AIhot {_AIHOT_LIMIT} + GitHub {_GH_LIMIT}）", file=sys.stderr)
