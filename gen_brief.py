#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「今日关注」简报的输入数据（facts）。
从所有数据源抽取结构化事实，供 AI 生成简报。

数据源：待办 / 直播 / 视频(博主更新) / 产品动态 / 豆瓣热门(书影音) /
        我的影音库 / 新闻(news) / KM热榜文章 / 知识库 / 笔记 / 书签
"""
import json, os, datetime

DATA = os.environ.get("WORKTAB_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))

def read(name, default):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def iso(d):
    if not d:
        return None
    return str(d)[:10]

def main():
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    facts = {"date": today, "now_hhmm": now.strftime("%H:%M")}

    # 1) 待办（未完成）
    tasks = read("tasks.json", {"items": []}).get("items", [])
    undone = [t for t in tasks if not t.get("done")]
    facts["todos"] = [{"title": t.get("title", ""), "priority": t.get("priority", "中"), "date": iso(t.get("date"))} for t in undone]

    # 2) 直播课（未来）
    lives = read("qlearning_lives.json", {"items": []}).get("items", [])
    upcoming = []
    for l in lives:
        st = l.get("startTime")
        if not st:
            continue
        try:
            t0 = datetime.datetime.strptime(st.replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        if t0 >= now:
            upcoming.append({"title": l.get("title", ""), "author": l.get("author", ""), "start": st, "href": l.get("href", "")})
    upcoming.sort(key=lambda x: x["start"])
    facts["lives"] = upcoming[:10]

    # 3) 博主视频更新（近 3 天）
    vids = read("creator_videos.json", {}).get("items", [])
    three_days_ago = now - datetime.timedelta(days=3)
    recent = []
    for v in vids:
        pub = v.get("published_at")
        if not pub:
            continue
        try:
            t = datetime.datetime.fromtimestamp(int(pub))
        except Exception:
            continue
        if t >= three_days_ago:
            recent.append({"title": v.get("title", ""), "creator": v.get("creator_name", ""), "platform": v.get("platform", ""), "published": t.strftime("%Y-%m-%d"), "url": v.get("url", "")})
    recent.sort(key=lambda x: x["published"], reverse=True)
    facts["videos"] = recent[:20]

    # 4) 产品动态（近 5 天）
    updates = read("products_updates.json", {}).get("items", [])
    five_days_ago = (now - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    prods = []
    for u in updates:
        d = iso(u.get("date"))
        if d and d >= five_days_ago:
            prods.append({"product": u.get("product", ""), "type": u.get("type", ""), "date": d, "title": u.get("title", "")})
    prods.sort(key=lambda x: x["date"], reverse=True)
    facts["product_updates"] = prods[:15]
    facts["monitored_products"] = [p.get("name", "") for p in read("products.json", {}).get("products", [])]

    # 5) 豆瓣热门（书影音）
    douban = read("douban_hot.json", {}).get("items", {})
    db = {}
    for cat, items in douban.items():
        db[cat] = [{"title": it.get("title") or it.get("name", ""), "rating": it.get("rating") or it.get("score")} for it in (items or [])[:5]]
    facts["douban_hot"] = db

    # 6) 我的影音库
    media = read("media.json", {}).get("items", [])
    facts["media_lib"] = [{"title": m.get("title") or m.get("name", ""), "status": m.get("status", ""), "note": m.get("note", "")} for m in media]

    # 7) 新闻动态（news，GitHub + AI HOT）
    news = read("news.json", {}).get("items", [])
    facts["news"] = [{"source": n.get("source", ""), "title": n.get("title", ""), "desc": n.get("desc", ""), "date": iso(n.get("date"))} for n in news[:20]]

    # 8) KM 热榜文章（近 3 天）
    arts = read("articles.json", {}).get("items", [])
    three_days_ago_s = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    articles = [a for a in arts if iso(a.get("date")) and iso(a.get("date")) >= three_days_ago_s]
    articles.sort(key=lambda x: x.get("date", ""), reverse=True)
    facts["km_articles"] = [{"title": a.get("title", ""), "date": iso(a.get("date")), "hot": bool(a.get("hot")), "author": a.get("author", "")} for a in articles[:15]]

    # 9) 知识库 / 笔记 / 书签
    facts["knowledge"] = [{"title": k.get("title", ""), "kind": k.get("kind", "")} for k in read("knowledge.json", {}).get("items", [])]
    facts["notes"] = [{"title": n.get("title", ""), "topic": n.get("topic", "")} for n in read("notes.json", {}).get("items", [])]
    facts["bookmarks"] = [{"title": b.get("title", "") or b.get("name", ""), "url": b.get("url", "")} for b in read("bookmarks.json", {}).get("items", [])[:20]]

    out = "/tmp/brief_facts.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)
    print(json.dumps(facts, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
