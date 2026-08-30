#!/usr/bin/env python3
"""产品洞察候选源抓取模块（工作台自带，不依赖外部 Agent）。

数据源（抓取失败均静默降级，不影响整体）：
  - Reddit   : r/ProductManagement / r/startups / r/SaaS 热帖（RSS 接口，稳定）
  - 知乎     : 热榜（公开接口，可能偶尔受限，抓不到则跳过）
  - 少数派   : 热门文章（sspai 接口，效率工具/产品思考密度高）

输出：raw 候选列表，字段对齐 app.py 的 AI 提炼入口：
  title / url / source / source_label / raw_text

官方博客产品案例不在此抓取（复用 news.json 中的 product-case 条目），
由 app.py 侧读取后并入候选池。
"""
import json
import re
import html
import datetime
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _now():
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).isoformat(timespec="seconds")


def _get_text(url, timeout=15, ua=None, headers=None):
    """GET 返回解码文本；失败返回空串。"""
    req = urllib.request.Request(url, headers={"User-Agent": ua or _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        enc = resp.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(enc, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_json(url, timeout=12, ua=None, headers=None):
    """GET 并解析 JSON；失败返回 None。"""
    txt = _get_text(url, timeout=timeout, ua=ua, headers=headers)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


# ---------------------------------------------------------------------------
# Reddit 热帖（Atom RSS，规避 .json 对数据中心 IP 的 403）
# ---------------------------------------------------------------------------
_REDDIT_SUBREDDITS = ["ProductManagement", "startups", "SaaS"]
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_REDDIT_SKIP_RE = re.compile(
    r"career thread|rant thread|weekly|quarterly|megathread|welcome|"
    r"announcement|state of the|town hall|introductions|^rules|mod post",
    re.I,
)


def _fetch_reddit(limit_per_sub=6):
    out = []
    for sub in _REDDIT_SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/hot/.rss"
        txt = _get_text(url, ua=_UA)
        if not txt:
            continue
        try:
            root = ET.fromstring(txt)
        except Exception:
            continue
        entries = root.findall("a:entry", _ATOM_NS)
        got = 0
        for e in entries:
            if got >= limit_per_sub:
                break
            title_el = e.find("a:title", _ATOM_NS)
            title = (title_el.text or "").strip() if title_el is not None else ""
            if not title or _REDDIT_SKIP_RE.search(title):
                continue
            link_el = e.find("a:link", _ATOM_NS)
            link = link_el.get("href") if link_el is not None else ""
            content_el = e.find("a:content", _ATOM_NS)
            selftext = _strip_html(content_el.text or "") if content_el is not None else ""
            got += 1
            out.append({
                "title": title,
                "url": link,
                "source": "Reddit",
                "source_label": f"r/{sub}",
                "raw_text": (title + ("。 " + selftext[:300]) if selftext else title)[:600],
            })
    return out


# ---------------------------------------------------------------------------
# 知乎热榜
# ---------------------------------------------------------------------------
def _fetch_zhihu(limit=12):
    url = f"https://api.zhihu.com/topstory/hot-list?limit={limit}"
    data = _get_json(url, ua=_UA, headers={"x-api-version": "3.0.91", "x-app-za": "OS=iOS"})
    if not data:
        return []
    out = []
    for it in data.get("data") or []:
        tgt = it.get("target") or {}
        ta = tgt.get("title_area") or {}
        title = _strip_html(ta.get("text") or "")
        if not title:
            continue
        ea = tgt.get("excerpt_area") or {}
        excerpt = _strip_html(ea.get("text") or "")[:300]
        link = (tgt.get("link") or {}).get("url") or ""
        metrics = (tgt.get("metrics_area") or {}).get("text") or ""
        raw_text = title + (("。 " + excerpt) if excerpt else "")
        out.append({
            "title": title,
            "url": link,
            "source": "知乎",
            "source_label": "知乎热榜" + ((" · " + metrics) if metrics else ""),
            "raw_text": raw_text[:600],
        })
    return out


# ---------------------------------------------------------------------------
# 少数派热门文章
# ---------------------------------------------------------------------------
def _fetch_sspai(limit=12):
    url = ("https://sspai.com/api/v1/article/tag/page/get"
           "?limit=%d&offset=0&tag=%%E7%%83%%AD%%E9%%97%%A8%%E6%%96%%87%%E7%%AB%%A0" % limit)
    data = _get_json(url, ua=_UA)
    if not data:
        return []
    items = data.get("data") or []
    out = []
    for it in items:
        title = _strip_html(it.get("title") or "")
        if not title:
            continue
        a_id = it.get("id")
        link = f"https://sspai.com/post/{a_id}" if a_id else ""
        summary = _strip_html(it.get("summary") or "")[:300]
        author = (it.get("author") or {}).get("nickname") or ""
        raw_text = title + (("。 " + summary) if summary else "")
        out.append({
            "title": title,
            "url": link,
            "source": "少数派",
            "source_label": "少数派热门" + ((" · " + author) if author else ""),
            "raw_text": raw_text[:600],
        })
    return out


def fetch_candidates():
    """汇总各源候选，去重后返回。每个源独立失败，不影响整体。"""
    items = []
    for fn in (_fetch_reddit, _fetch_zhihu, _fetch_sspai):
        try:
            items += fn()
        except Exception as e:
            print(f"[insight_fetcher] {fn.__name__} 抓取失败: {e}")
    # 去重：按 url / title
    seen = set()
    cleaned = []
    for it in items:
        key = it.get("url") or it.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(it)
    return cleaned


if __name__ == "__main__":
    data = fetch_candidates()
    print(json.dumps(data, ensure_ascii=False, indent=2))
