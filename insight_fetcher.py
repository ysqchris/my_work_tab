#!/usr/bin/env python3
"""产品观察候选源：人人都是产品经理、知乎、少数派。

仅使用国内社区公开接口，不访问 Reddit 或混入官方博客资讯。
输出字段：title / url / source / source_label / raw_text。
各来源独立失败降级，不生成或补造素材。
"""
import concurrent.futures
import html
import json
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_PRODUCT_RE = re.compile(
    r"产品|用户|运营|商业|创业|营销|增长|订阅|付费|定价|平台|电商|消费|"
    r"品牌|设计|交互|体验|软件|应用|工具|效率|互联网|社交|短视频|直播|"
    r"人工智能|大模型|智能体|算法|推荐|搜索|广告|小程序|"
    r"微信|抖音|小红书|淘宝|京东|美团|拼多多|豆包|腾讯|阿里|字节|"
    r"\bAI\b|\bSaaS\b|\bApp\b",
    re.I,
)


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
    except Exception as e:
        print(f"[insight_fetcher] {urllib.parse.urlsplit(url).hostname} 抓取失败: {e}", flush=True)
        return ""


def _get_json(url, timeout=12, ua=None, headers=None):
    txt = _get_text(url, timeout=timeout, ua=ua, headers=headers)
    try:
        return json.loads(txt) if txt else None
    except (ValueError, TypeError):
        return None


def _strip_html(s):
    text = html.unescape(str(s or ""))
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _candidate(title, link, source, label, summary=""):
    title, summary = _strip_html(title), _strip_html(summary)[:500]
    if not title or not re.match(r"^https?://", link or "", re.I):
        return None
    return {
        "title": title,
        "url": link,
        "source": source,
        "source_label": label,
        "raw_text": (title + ("。 " + summary if summary else ""))[:700],
    }


def _fetch_rss(url, source, label, limit=12, product_only=False):
    """共享 RSS/Atom 解析，使用来源提供的真实摘要或正文片段。"""
    text = _get_text(url)
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall("./channel/item") or root.findall(atom + "entry")
    out = []
    for entry in entries:
        title = entry.findtext("title") or entry.findtext(atom + "title") or ""
        link = entry.findtext("link") or ""
        if not link:
            link_el = entry.find(atom + "link")
            link = link_el.get("href", "") if link_el is not None else ""
        summary = (entry.findtext("description")
                   or entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                   or entry.findtext(atom + "summary") or entry.findtext(atom + "content") or "")
        categories = " ".join(entry.itertext()) if product_only else ""
        if product_only and not _PRODUCT_RE.search(title + " " + _strip_html(summary)[:500] + " " + categories[:500]):
            continue
        item = _candidate(title, link, source, label, summary)
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _fetch_woshipm(limit=12):
    return _fetch_rss("https://www.woshipm.com/feed", "人人都是产品经理", "人人都是产品经理 · 最新文章", limit)


def _fetch_zhihu(limit=12):
    data = _get_json("https://api.zhihu.com/topstory/hot-list?limit=50",
                     headers={"x-api-version": "3.0.91", "x-app-za": "OS=iOS"})
    if not isinstance(data, dict):
        return []
    out = []
    for it in data.get("data") or []:
        tgt = it.get("target") or {}
        title = _strip_html(tgt.get("title") or (tgt.get("title_area") or {}).get("text"))
        excerpt = _strip_html(tgt.get("excerpt") or (tgt.get("excerpt_area") or {}).get("text"))
        if not _PRODUCT_RE.search(title + " " + excerpt):
            continue
        link = (tgt.get("link") or {}).get("url") or tgt.get("url") or ""
        question_id = tgt.get("id")
        if question_id and str(question_id).isdigit():
            link = f"https://www.zhihu.com/question/{question_id}"
        else:
            link = re.sub(r"^https://api\.zhihu\.com/questions/", "https://www.zhihu.com/question/", link)
        metrics = it.get("detail_text") or (tgt.get("metrics_area") or {}).get("text") or ""
        item = _candidate(title, link, "知乎", "知乎 · 产品相关热议" + (" · " + metrics if metrics else ""), excerpt)
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _fetch_sspai(limit=12):
    return _fetch_rss("https://sspai.com/feed", "少数派", "少数派 · 数字产品与效率", limit, product_only=True)


def fetch_candidates():
    """并行抓取，交错合并各社区，避免 AI 输入上限挤掉后面的来源。"""
    fetchers = (_fetch_woshipm, _fetch_zhihu, _fetch_sspai)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = [pool.submit(fn) for fn in fetchers]
        groups = []
        for fn, future in zip(fetchers, futures):
            try:
                groups.append(future.result())
            except Exception as e:
                print(f"[insight_fetcher] {fn.__name__} 抓取失败: {e}", flush=True)
                groups.append([])
    seen, cleaned = set(), []
    for i in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if i >= len(group):
                continue
            item = group[i]
            key = item.get("url") or item.get("title")
            if key and key not in seen:
                seen.add(key)
                cleaned.append(item)
    return cleaned


if __name__ == "__main__":
    print(json.dumps(fetch_candidates(), ensure_ascii=False, indent=2))
