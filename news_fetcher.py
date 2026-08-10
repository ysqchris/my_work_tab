#!/usr/bin/env python3
"""新闻动态本地抓取模块（工作台自带，不依赖外部 Agent）。

数据源：
  - IT之家 RSS  (https://www.ithome.com/rss/)         -> 科技快讯
  - GitHub Trending (https://github.com/trending)      -> 热门开源项目

输出：与 news.json 结构一致的 items 列表，字段对齐前端展示：
  title / url / date / desc / source / topic / id / created_at
"""
import json
import re
import html
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 各源的专题（topic）与来源名（source）
IT_HOME_TOPIC = "科技快讯"
IT_HOME_SOURCE = "IT之家"
GH_TOPIC = "GitHub 热榜"
GH_SOURCE = "GitHub Trending"

_ITHOME_RSS = "https://www.ithome.com/rss/"
_GH_TRENDING = "https://github.com/trending"


def _now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


def _today():
    return _now()[:10]


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # 尝试按声明编码解码，失败则按 utf-8 容错
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
    import uuid
    return uuid.uuid5(uuid.NAMESPACE_URL, str(seed)).hex[:12]


def fetch_ithome(limit=8):
    """抓取 IT之家 RSS，返回 items。"""
    items = []
    try:
        xml_text = _get(_ITHOME_RSS)
        root = ET.fromstring(xml_text)
        # RSS 2.0: rss/channel/item
        channel = root.find("channel")
        if channel is None:
            return items
        for entry in channel.findall("item")[:limit]:
            title_el = entry.find("title")
            link_el = entry.find("link")
            desc_el = entry.find("description")
            pub_el = entry.find("pubDate")
            title = _strip_tags(_elem_text(title_el))
            url = _strip_tags(_elem_text(link_el))
            desc = _strip_tags(_elem_text(desc_el))
            if desc:
                desc = desc[:160]
            date = _parse_rfc822(pub_el.text) if pub_el is not None and pub_el.text else _today()
            if not title or not url:
                continue
            items.append({
                "title": title,
                "url": url,
                "date": date,
                "desc": desc,
                "source": IT_HOME_SOURCE,
                "topic": IT_HOME_TOPIC,
                "id": _gen_id(url or title),
                "created_at": _now(),
            })
    except Exception as e:
        print(f"[news_fetcher] IT之家抓取失败: {e}")
    return items


def fetch_github_trending(limit=8):
    """抓取 GitHub Trending，返回 items。"""
    items = []
    try:
        html_text = _get(_GH_TRENDING)
        # 每个仓库卡片：<article class="Box-row">
        blocks = re.findall(r"<article class=\"Box-row\">(.*?)</article>", html_text, re.S)
        for block in blocks[:limit]:
            # 仓库名：<h2 class="h3 lh-condensed"> ... <a href="/owner/repo"> ...
            m = re.search(r"<h2[^>]*>.*?<a[^>]*href=\"([^\"]+)\"", block, re.S)
            if not m:
                continue
            repo_path = m.group(1).strip()
            if repo_path.startswith("/"):
                repo_path = repo_path[1:]
            # 描述
            dm = re.search(r"<p class=\"col-9 color-fg-muted my-1 pr-4\">\s*(.*?)\s*</p>", block, re.S)
            desc = _strip_tags(dm.group(1)) if dm else ""
            # 语言
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


def _elem_text(el):
    if el is None:
        return ""
    return "".join(el.itertext())


def _parse_rfc822(text):
    """把 RSS 的 RFC822 时间转成 YYYY-MM-DD（按东八区当天近似，简单截断日期）。"""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return _today()


def fetch_all():
    """汇总所有源，去重后返回 items（最新在前）。"""
    items = []
    items += fetch_ithome(limit=8)
    items += fetch_github_trending(limit=8)
    # 去重：同 url 或同 title 只保留第一条
    seen = set()
    cleaned = []
    for it in items:
        key = it.get("url") or it.get("title")
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(it)
    # 倒序：date 新的在前
    cleaned.sort(key=lambda x: (x.get("date") or "", x.get("created_at") or ""), reverse=True)
    return cleaned


if __name__ == "__main__":
    import sys
    data = fetch_all()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n共抓取 {len(data)} 条", file=sys.stderr)
