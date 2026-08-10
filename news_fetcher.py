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


def _gen_id(seed):
    return uuid.uuid5(uuid.NAMESPACE_URL, str(seed)).hex[:12]


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
    """抓取 GitHub Trending，取最热的前 limit 个仓库。"""
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
            dm = re.search(r"<p class=\"col-9 color-fg-muted my-1 pr-4\">\s*(.*?)\s*</p>", block, re.S)
            desc = _strip_tags(dm.group(1)) if dm else ""
            lm = re.search(r"<span itemprop=\"programmingLanguage\">([^<]+)</span>", block)
            lang = lm.group(1).strip() if lm else ""
            title = repo_path
            url = "https://github.com/" + repo_path
            summary = desc
            if lang:
                summary = f"[{lang}] " + (summary or "")
            items.append({
                "title": title,
                "url": url,
                "date": _today(),
                "desc": summary[:160],
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
