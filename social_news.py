"""Collect and select recent news from public publisher feeds and sections."""
import datetime as dt
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from news_fetcher import _parse_rss, _post_json, _strip_tags

LOCAL_TIME = dt.timezone(dt.timedelta(hours=8))
REFRESH_SECONDS = 30 * 60
DAILY_LIMIT = 20
CACHE_NAME = "social-news.json"
CCTV_FEEDS = "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/"
CCTV_ARTICLE = r"/\d{4}/\d{2}/\d{2}/ARTI\w+\.shtml"
UA = "Mozilla/5.0"


@dataclass(frozen=True)
class Source:
    name: str
    category: str
    url: str
    format: str
    article_path: str = ""
    overseas: bool = False


SOURCES = (
    Source("央视新闻", "world", CCTV_FEEDS + "world_1.jsonp", "jsonp"),
    Source("新华社", "world", "https://www.news.cn/world/", "html", r"/world/\d{8}/\w+/c\.html"),
    Source("央视财经", "finance", CCTV_FEEDS + "economy_zixun_1.jsonp", "jsonp"),
    Source("华尔街见闻", "finance", "https://wallstreetcn.com/", "html", r"/articles/\d+"),
    Source("新华科技", "finance", "https://www.news.cn/tech/", "html", r"/tech/\d{8}/\w+/c\.html"),
    Source("36氪", "finance", "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot", "36kr"),
    Source("央视新闻", "people", CCTV_FEEDS + "society_1.jsonp", "jsonp"),
    Source("新华社", "people", "https://www.news.cn/politics/", "html", r"/politics/\d{8}/\w+/c\.html"),
    Source("南方周末", "people", "https://www.infzm.com/", "html", r"/contents/\d+"),
    Source("央视体育", "sports", "https://sports.cctv.com/", "html", CCTV_ARTICLE),
    Source("新华社", "sports", "https://www.news.cn/sports/", "html", r"/sports/\d{8}/\w+/c\.html"),
    Source("卫报", "world", "https://www.theguardian.com/world/rss", "rss", overseas=True),
    Source("法国世界报", "world", "https://www.lemonde.fr/en/international/rss_full.xml", "rss", overseas=True),
)
SOURCE_SECTIONS = {(source.name, source.category) for source in SOURCES}

# These are selection signals. The same topic alone never makes two stories duplicates.
IMPACT = {
    "world": re.compile(r"峰会|会晤|外交|停火|谈判|和平协议|制裁|关税|选举|大选|联合国|"
                        r"安理会|军事|军演|袭击|空袭|冲突|战事|撤军|地震|台风|洪灾|"
                        r"国际刑事法院|核协议|领空|人道|政变", re.I),
    "finance": re.compile(r"美联储|央行|利率|加息|降息|通胀|CPI|GDP|就业数据|财政|贸易|关税|"
                          r"财报|监管|上市|并购|收购|融资|芯片|半导体|航天|突破|大模型|"
                          r"机器人|能源|原油|银行|股市|工业|消费|汇率|国债", re.I),
    "people": re.compile(r"医保|社保|养老|托育|就业|高考|毕业生|教育|医疗|疫苗|新规|"
                         r"住房|户籍|食品安全|公共安全|台风|洪水|地震|寒潮|预警|"
                         r"救援|司法|判决|公益|交通|铁路|治理|儿童|劳动|民生"),
    "sports": re.compile(r"夺冠|冠军|决赛|破纪录|世界纪录|晋级|出线|全运会|奥运|亚运|世界杯|"
                         r"世锦赛|中超|欧冠|国足|中国队|国乒|NBA|CBA|F1|大满贯|总决赛", re.I),
}
OVERSEAS_IMPACT = re.compile(
    r"\b(summit|ceasefire|peace talks|sanctions?|tariffs?|elections?|earthquake|"
    r"airstrikes?|invasion|military|united nations|security council|coup|floods?|"
    r"humanitarian|parliament|G20|APEC|NATO)\b", re.I,
)
DEEP = re.compile(r"深度(?:报道|调查|解读|分析)|调查报告|专访|独家访谈|解读|观察|复盘|解析")
LOW_SIGNAL = re.compile(
    r"组图|图集|直播预告|看点前瞻|一图看懂|集锦|短视频|盘前必读|午评|早报|晚报|热榜|"
    r"8点1氪|氪星晚报|上新了|体验评测|上手体验|开箱|招生广告|知识竞赛|通知公告|"
    r"\blive updates\b|\bas it happened\b", re.I,
)
DATE_PATH = re.compile(r"/(20\d{2})[/-]?(\d{2})[/-]?(\d{2})/")
MINOR_QUAKE = re.compile(r"(\d+(?:\.\d+)?)\s*级地震")
INCIDENT_FOLLOWUP = re.compile(r"(?:事件|事故)?(?:已?致|造成)\d+[人死受伤亡失联、和]+$|"
                               r"(?:事件|事故)?(?:受伤|死亡|遇难|失联)人数(?:已|升至|增至|达)*\d+人?$")


def read_cache(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _date_window(now):
    return (now.date() - dt.timedelta(days=2)).isoformat(), now.date().isoformat()


def _normalized_time(value):
    try:
        if isinstance(value, (float, int)):
            date = dt.datetime.fromtimestamp(value / 1000, LOCAL_TIME)
        else:
            date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            date = date.replace(tzinfo=date.tzinfo or LOCAL_TIME).astimezone(LOCAL_TIME)
        return date.isoformat(timespec="seconds")
    except (AttributeError, ValueError, OverflowError, OSError):
        return ""


def _plain(value):
    return " ".join(_strip_tags(value or "").split())


def _summary(value, title):
    value = _plain(value).removeprefix("Continue reading").removesuffix("Continue reading").strip()
    if value.startswith(title):
        value = value[len(title):].lstrip(" -—：:。")
    if not value or _title_key(value) == _title_key(title):
        return ""
    limit = 300 if re.search(r"[\u4e00-\u9fff]", value) else 450
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _get(url):
    with urlopen(Request(url, headers={"User-Agent": UA}), timeout=9) as response:
        return response.read(2_500_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")


class PublisherPage(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.links = []
        self.meta = {}
        self.times = []
        self.paragraphs = []
        self.heading = ""
        self.heading_parts = None
        self.anchor = None
        self.paragraph = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.anchor = [attrs.get("href", ""), []]
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name") or ""
            self.meta[key.lower()] = attrs.get("content", "")
        elif tag == "time" and attrs.get("datetime"):
            self.times.append(attrs["datetime"])
        elif tag == "span" and attrs.get("data-time") and "nfzm-content__publish" in attrs.get("class", "").split():
            self.times.append(attrs["data-time"])
        elif tag == "p":
            self.paragraph = []
        elif tag == "h1" and not self.heading:
            self.heading_parts = []

    def handle_data(self, text):
        if self.anchor is not None:
            self.anchor[1].append(text)
        if self.paragraph is not None:
            self.paragraph.append(text)
        if self.heading_parts is not None:
            self.heading_parts.append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            href, parts = self.anchor
            self.links.append((href, " ".join("".join(parts).split())))
            self.anchor = None
        elif tag == "p" and self.paragraph is not None:
            self.paragraphs.append(" ".join("".join(self.paragraph).split()))
            self.paragraph = None
        elif tag == "h1" and self.heading_parts is not None:
            self.heading = " ".join("".join(self.heading_parts).split())
            self.heading_parts = None


def _url_key(url):
    parsed = urlsplit(url)
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _title_key(title):
    title = re.sub(r"^(?:新华社消息|新华社快讯|新华社权威快报|权威快报|快讯|独家|突发)[｜丨|：:\s]+", "", title)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", title.casefold())


def _incident_key(title):
    short = INCIDENT_FOLLOWUP.sub("", title)
    if short == title:
        return ""
    return _title_key(short.replace("发生", "").replace("一省政府", "省政府"))


def _item(source, title, url, published_at, rank, desc=""):
    # Entries are anchored to the publisher's publication time.
    title = _plain(title).removesuffix(" - 华尔街见闻").strip()
    title = re.sub(r"\s+20\d{2}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?$", "", title)
    if len(title) < 6 or not urlsplit(url).hostname or urlsplit(url).scheme not in ("http", "https"):
        return None
    when = _normalized_time(published_at)
    summary = _summary(desc, title)
    return {
        "title": title, "url": _url_key(url), "published_at": when, "date": when[:10],
        "desc": summary, "source": source.name, "category": source.category,
        "rank": rank, "overseas": source.overseas,
    }


def _page_candidates(source, content, earliest, today):
    result = {}
    host = urlsplit(source.url).hostname
    for href, title in PublisherPage(content).links:
        if not href or len(title) < 6 or LOW_SIGNAL.search(title):
            continue
        url = urlsplit(urljoin(source.url, href))
        if url.hostname != host or not re.fullmatch(source.article_path, url.path):
            continue
        date = DATE_PATH.search(url.path)
        day_hint = "-".join(date.groups()) if date else ""
        if day_hint and not earliest <= day_hint <= today:
            continue
        key = _url_key(url.geturl())
        if key not in result:
            result[key] = (source, title, key, day_hint, len(result))
        if len(result) >= 28:
            break
    return list(result.values())


def _collect_source(source, earliest, today):
    if source.format == "36kr":
        body = {"partner_id": "web", "timestamp": 0, "param": {"siteId": 1, "platformId": 2}}
        data = json.loads(_post_json(source.url, body, timeout=9, ua=UA))
        rows = data["data"]["hotRankList"]
        return [
            _item(source, row["templateMaterial"]["widgetTitle"], f"https://36kr.com/p/{row['itemId']}",
                  row.get("publishTime") or row["templateMaterial"]["publishTime"], rank)
            for rank, row in enumerate(rows) if row.get("itemId") and row.get("templateMaterial", {}).get("widgetTitle")
        ], []

    content = _get(source.url)
    if source.format == "html":
        candidates = _page_candidates(source, content, earliest, today)
        return [], candidates
    if source.format == "jsonp":
        payload = content[content.index("{"):content.rindex("}") + 1]
        rows = json.loads(payload)["data"]["list"]
        return [
            _item(source, row.get("title", ""), row.get("url", ""), row.get("focus_date", ""), rank, row.get("brief", ""))
            for rank, row in enumerate(rows) if row.get("url")
        ], []

    items = _parse_rss(content, source.url, 80)
    if not items:
        raise ValueError("empty or invalid RSS")
    return [
        _item(source, row["title"], row["url"], row.get("published_at", ""), rank, row.get("desc", ""))
        for rank, row in enumerate(items)
    ], []


def _collect_detail(candidate, previous):
    source, title, url, day_hint, rank = candidate
    prior = previous.get((source.name, source.category, url))
    if prior and prior.get("desc"):
        return {**prior, "rank": rank}
    try:
        page = PublisherPage(_get(url))
    except Exception as error:
        logging.info("[social-news] article unavailable on %s: %s", source.name, error)
        return _item(source, title, url, day_hint, rank)
    meta = page.meta
    published_at = (meta.get("article:published_time") or meta.get("pubdate") or meta.get("publishdate")
                    or next(iter(page.times), "") or day_hint)
    title = page.heading or meta.get("og:title") or title
    description = _summary(meta.get("og:description") or meta.get("description"), title)
    if not description and source.name in {"新华社", "新华科技", "央视体育"}:
        # These pages sometimes put only the headline in description.
        description = next((p for p in page.paragraphs if len(p) >= 48 and not p.startswith(
            ("责任编辑", "版权所有", "Copyright", "扫一扫", "未经授权"))), "")
    return _item(source, title, url, published_at, rank, description)


def _editorial_score(item):
    title = item["title"]
    category = item["category"]
    if LOW_SIGNAL.search(title):
        return None
    quake = MINOR_QUAKE.search(title)
    if quake and float(quake[1]) < 5 and not re.search(r"伤亡|受伤|遇难|救援|损失", title):
        return None
    source_rank = item["rank"]
    relevance = IMPACT[category]
    if item["overseas"]:
        if not OVERSEAS_IMPACT.search(title):
            return None
        return 20 - min(source_rank, 18)
    headline_hits = len(set(relevance.findall(title)))
    if not headline_hits:
        if item["source"] in {"36氪", "南方周末"} or source_rank >= 8:
            return None
        if not item["desc"]:
            return None
    context = bool(relevance.search(item["desc"]))
    return (10 * min(headline_hits, 3) + 3 * context
            + max(0, 8 - source_rank) + 3 * (item["kind"] == "deep"))


def _priority(item):
    return item["_score"], len(item["desc"]) >= 45, not item["overseas"], item["published_at"]


def _select_daily(candidates, now):
    earliest, today = _date_window(now)
    ranked = []
    for item in candidates:
        if not earliest <= item["date"] <= today:
            continue
        kind = "deep" if DEEP.search(item["title"]) or item["source"] == "南方周末" else "latest"
        item = {**item, "kind": kind}
        score = _editorial_score(item)
        if score is not None:
            ranked.append({**item, "_score": score})
    ranked.sort(key=_priority, reverse=True)

    section_counts = Counter()
    overseas_counts = Counter()
    seen_titles = set()
    seen_incidents = set()
    similar_titles = {}
    seen_urls = set()
    selected = []
    for item in ranked:
        day, category = item["date"], item["category"]
        key = (day, category)
        # Keep most of the international section for domestic reporting.
        if section_counts[key] >= DAILY_LIMIT or item["overseas"] and overseas_counts[day] >= 3:
            continue
        title = _title_key(item["title"])
        incident = _incident_key(item["title"])
        if item["url"] in seen_urls or title in seen_titles or (day, incident) in seen_incidents:
            continue
        if any(min(len(title), len(prior)) >= 14 and abs(len(title) - len(prior)) <= 6
               and (title in prior or prior in title)
               for prior in similar_titles.get(key, ())):
            continue
        seen_urls.add(item["url"])
        seen_titles.add(title)
        if incident:
            seen_incidents.add((day, incident))
        similar_titles.setdefault(key, []).append(title)
        section_counts[key] += 1
        if item["overseas"]:
            overseas_counts[day] += 1
        selected.append({field: item[field] for field in
                         ("title", "url", "date", "published_at", "desc", "source", "category", "kind")})
    return sorted(selected, key=lambda item: item["date"], reverse=True)


def snapshot(cache, now=None):
    now = now or dt.datetime.now(LOCAL_TIME)
    return {
        "updated_at": cache.get("updated_at", ""),
        "items": _select_daily(cache.get("candidates", []), now),
        "partial": bool(cache.get("failed_sources")),
    }


def _fetch_source(source, earliest, today):
    try:
        return _collect_source(source, earliest, today)
    except Exception as error:
        logging.warning("[social-news] %s/%s unavailable: %s", source.name, source.category, error)
        return [], []


def refresh(path):
    path = Path(path)
    cache = read_cache(path)
    now = dt.datetime.now(LOCAL_TIME)
    earliest, today = _date_window(now)
    previous = {(item["source"], item["category"], item["url"]): item for item in cache.get("candidates", [])
                if earliest <= item["date"] <= today and (item["source"], item["category"]) in SOURCE_SECTIONS}
    incoming, details, failed_sources = [], [], []
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda source: _fetch_source(source, earliest, today), SOURCES))
        for source, (items, candidates) in zip(SOURCES, results):
            incoming.extend(item for item in items if item)
            details.extend(candidates)
            if not items and not candidates:
                failed_sources.append(f"{source.name}/{source.category}")
        incoming.extend(item for item in executor.map(lambda candidate: _collect_detail(candidate, previous), details)
                        if item)
    recent = [item for item in incoming if earliest <= item["date"] <= today]
    merged = dict(previous)
    for item in recent:
        key = (item["source"], item["category"], item["url"])
        old = merged.get(key, {})
        merged[key] = {**item, "desc": item["desc"] or old.get("desc", "")}
    if not recent:
        logging.warning("[social-news] no new publishable data, retaining last snapshot")
        return snapshot(cache, now)
    cache = {
        "updated_at": now.isoformat(timespec="seconds"),
        "candidates": list(merged.values()),
        "failed_sources": failed_sources,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    file = NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False)
    temp_path = Path(file.name)
    try:
        with file:
            json.dump(cache, file, ensure_ascii=False)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return snapshot(cache, now)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path, help="Path to the social-news cache")
    args = parser.parse_args()
    result = refresh(args.cache)
    counts = Counter((item["date"], item["category"]) for item in result["items"])
    for (day, category), count in sorted(counts.items(), reverse=True):
        print(day, category, count)
