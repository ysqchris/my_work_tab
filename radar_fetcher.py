#!/usr/bin/env python3
"""鹰眼（竞品动态）抓取模块（工作台自带，不依赖外部 Agent）。

只负责「抓」，不调大模型。从各竞品官方 changelog / release 页抓取版本更新
原始条目，输出 raw 列表，字段对齐 app.py 的鹰眼提炼入口：
  product / type / date / title / url / raw_text

覆盖源（外网 changelog，纯 HTTP 可及）：
  - WorkBuddy : codebuddy.cn/docs/workbuddy/Changelog（Docusaurus 文档站，可抓正文）
  - Multica   : multica.ai/changelog（可抓版本、标题与日期映射）
  - CrewAI    : GitHub releases（优先官方 API 拿正文，失败回退 HTML 解析）

其余竞品（QoderWake / Jira / 千问办公 / 豆包工作 / Taskade / ClickUp / Box-AI 等）
因官网 changelog 无稳定公开接口、需 JS 渲染或需内网鉴权，暂不纳入本地抓取，
继续靠外部 Agent 经 POST /api/products_updates 补充。
"""
import json
import re
import os
import datetime
import importlib.util
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_news_fetcher():
    """复用同目录 news_fetcher.py 的 HTTP 工具（DRY）。"""
    spec = importlib.util.spec_from_file_location(
        "news_fetcher", os.path.join(_HERE, "news_fetcher.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_nf = _load_news_fetcher()
_get = _nf._get
_get_via_curl = _nf._get_via_curl
_UA = _nf._UA
_today = _nf._today


def _strip_html(s):
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S)
    s = re.sub(r"<style.*?</style>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    import html as _html
    return _html.unescape(s)


def _clean(s):
    s = (s or "").replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", s).strip()


def _item(product, title, url, date="", raw_text=""):
    return {
        "product": product,
        "type": "产品动态",
        "date": date or _today(),
        "title": _clean(title),
        "url": url,
        "raw_text": _clean(raw_text)[:900],
    }


# ---------------------------------------------------------------------------
# WorkBuddy：Docusaurus 文档站，h2 = 版本标题，标题后文本 = 该版本功能点
# ---------------------------------------------------------------------------
_WB_VER_RE = re.compile(r"<h2[^>]*>(.*?)</h2>(.*?)(?=<h2[^>]*>|$)", re.S)


def _fetch_workbuddy(limit=3):
    base = "https://www.codebuddy.cn/docs/workbuddy/Changelog"
    txt = _get(base, ua=_UA) or _get_via_curl(base)
    if not txt:
        return []
    out = []
    for m in _WB_VER_RE.finditer(txt):
        title = _clean(_strip_html(m.group(1)))
        body = _clean(_strip_html(m.group(2)))
        if "版本发布" not in title and not re.search(r"\d+\.\d+", title):
            continue
        dm = re.search(r"（\s*(\d{4}-\d{2}-\d{2})\s*）", title)
        date = dm.group(1) if dm else _today()
        # 标题统一去掉表情与多余空白，保留版本号
        title = re.sub(r"\s*🚀\s*", " ", title).strip()
        anchor = re.sub(r"\s+", "-", title).lower()
        url = base + "#" + anchor
        out.append(_item("WorkBuddy", title, url, date, body))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Multica：时间轴「August 2026 31 v 0.4.37」给出日期，h2 给出标题与正文
# ---------------------------------------------------------------------------
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MULTICA_VER_RE = re.compile(r"release-0-4-(\d+)")
_MULTICA_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>(.*?)(?=<h2[^>]*>|$)", re.S)


def _multica_date_map(txt):
    """从时间轴文本解析 {版本号: 日期}。"""
    mmap = {}
    plain = _clean(_strip_html(txt))
    # 月份分组：August 2026 31 v 0.4.37 28 v 0.4.36 ...
    month_re = re.compile(r"(" + "|".join(_MONTHS.keys()) + r")\s+(\d{4})", re.I)
    pos = 0
    groups = list(month_re.finditer(plain))
    for i, gm in enumerate(groups):
        month = _MONTHS[gm.group(1).lower()]
        year = int(gm.group(2))
        seg_end = groups[i + 1].start() if i + 1 < len(groups) else len(plain)
        seg = plain[gm.end():seg_end]
        for vm in re.finditer(r"(\d{1,2})\s+v\s*0\.4\.(\d+)", seg):
            day = int(vm.group(1))
            ver = vm.group(2)
            mmap[ver] = f"{year:04d}-{month:02d}-{day:02d}"
    return mmap


def _fetch_multica(limit=3):
    base = "https://multica.ai/changelog"
    txt = _get(base, ua=_UA)
    if not txt:
        return []
    dmap = _multica_date_map(txt)
    # 版本锚点与 h2 标题均按出现顺序 index 配对（与 news_fetcher._parse_multica 一致）
    anchors = [m.group(1) for m in _MULTICA_VER_RE.finditer(txt)]
    h2blocks = []
    for m in _MULTICA_H2_RE.finditer(txt):
        title = _clean(_strip_html(m.group(1)))
        body = _clean(_strip_html(m.group(2)))
        if not title or title.lower() in ("all releases", "changelog"):
            continue
        h2blocks.append((title, body))
    out = []
    for i, ver in enumerate(anchors[:limit]):
        if i >= len(h2blocks):
            break
        title, body = h2blocks[i]
        date = dmap.get(ver, _today())
        url = f"{base}#release-0-4-{ver}"
        out.append(_item("Multica", title, url, date, body))
    return out


# ---------------------------------------------------------------------------
# CrewAI：优先 GitHub API（拿正文 + 精确日期），失败回退 HTML 解析
# ---------------------------------------------------------------------------
_GITHUB_API = "https://api.github.com/repos/crewAIInc/crewAI/releases"


def _fetch_crewai(limit=3):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(_GITHUB_API + "?per_page=%d" % limit, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        out = []
        for r in data or []:
            ver = (r.get("tag_name") or "").strip()
            name = (r.get("name") or "").strip()
            body = (r.get("body") or "").strip()
            url = r.get("html_url") or ""
            published = (r.get("published_at") or "")[:10]
            title = name or (f"CrewAI {ver} 发布" if ver else "CrewAI 发布")
            out.append(_item("CrewAI", title, url, published, body))
        return out
    except Exception as e:
        print(f"[radar_fetcher] CrewAI API 抓取失败，回退 HTML: {e}", flush=True)
    # 回退：HTML 解析版本号（无正文）
    base = "https://github.com/crewAIInc/crewAI/releases"
    txt = _get(base, ua=_UA) or _get_via_curl(base)
    if not txt:
        return []
    out = []
    seen = set()
    for m in re.finditer(r"releases/tag/([0-9.]+)", txt):
        ver = m.group(1)
        if ver in seen:
            continue
        seen.add(ver)
        out.append(_item("CrewAI", f"CrewAI v{ver} 发布",
                         f"https://github.com/crewAIInc/crewAI/releases/tag/{ver}"))
        if len(out) >= limit:
            break
    return out


def fetch_radar_raw():
    """汇总各竞品 changelog 原始条目，去重后返回。"""
    items = []
    for fn in (_fetch_workbuddy, _fetch_multica, _fetch_crewai):
        try:
            items += fn()
        except Exception as e:
            print(f"[radar_fetcher] {fn.__name__} 抓取失败: {e}", flush=True)
    seen = set()
    cleaned = []
    for it in items:
        key = it.get("url") or it.get("title")
        if not key or key in seen:
            continue
        if not it.get("title"):
            continue
        seen.add(key)
        cleaned.append(it)
    return cleaned


if __name__ == "__main__":
    data = fetch_radar_raw()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n共抓取 {len(data)} 条", flush=True)
