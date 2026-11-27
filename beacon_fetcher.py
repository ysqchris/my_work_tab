"""IMate 运营数据抓取模块：直连灯塔 DataTalk MCP，拉取团队协作看板的关键图卡。

协议说明：
- 灯塔 MCP 服务端点 https://beacon.mcp.it.woa.com/（streamable-http，JSON-RPC 2.0）
- 鉴权：Authorization: Bearer <太湖个人令牌 TAI_IT_TOKEN>
- 流程：initialize -> notifications/initialized -> tools/call
- 响应为 text/event-stream，data: 行内是 JSON-RPC 结果；业务数据在 result.content[0].text

看板：gongfeng / IMate - 团队协作（pageId=365227）
数据按"1 天 / 7 天 / 30 天"三套拉取，落库后前端直接切换展示，无需二次请求。

限流策略：灯塔 mcp-query 按用户限流（"请求量太多"）。因此本模块采用
"单 session 复用 + 串行 + 请求间隔"的方式，避免每个图卡重新握手产生 3 倍
请求量，并控制单位时间请求频率。每日定时跑一次，总耗时约 1 分钟可接受。
"""
import os
import json
import re
import time
import datetime
import urllib.request

BASE = "https://beacon.mcp.it.woa.com/"
BIZ_ID = "gongfeng"
# 项目协作看板 372662（IMate - 团队协作（新））。旧看板 365227 已下线，
# 图卡 ID 与变量池整体迁移；详情页：
# https://beacon.woa.com/datatalk/gongfeng/dashboard/372662?menuIds=menu_v2206h06&paramsid=f4e32320af1e0bdc3ac216a729e73c50
PAGE_ID = 372662
PARAMS_ID = "f4e32320af1e0bdc3ac216a729e73c50"
DATE_VAR = "selected_date_range"
_LLM_MODEL = "hy4-flash"

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
REQUEST_INTERVAL = 1.2  # 每次查询前的间隔秒数，规避限流


def _load_env():
    """从项目根目录 .env 读取 KEY=VALUE，仅补充缺失项。"""
    try:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.isfile(env_path):
            return
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and not os.environ.get(k, "").strip():
                    os.environ[k] = v
    except Exception:
        pass


_load_env()


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
    resp = urllib.request.urlopen(req, timeout=120)
    sid = resp.headers.get("Mcp-Session-Id")
    body = resp.read().decode("utf-8")
    return sid, body


def _parse_sse(body):
    """解析 streamable-http 的 text/event-stream 响应，返回 JSON-RPC 对象。"""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body)


class _BeaconSession:
    """维护一个 MCP session，串行复用，避免每个图卡重复握手。"""

    def __init__(self):
        self.sid = None

    def ensure(self):
        if self.sid:
            return
        sid, _ = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "worktable", "version": "1.0.0"}},
        })
        self.sid = sid
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.sid)

    def invalidate(self):
        self.sid = None

    def query(self, card_id, variables=None, page_id=PAGE_ID, params_id=PARAMS_ID):
        """查询单个图卡，返回业务 data 字典。限流/会话失效时抛异常。"""
        self.ensure()
        card_query = {"pageId": page_id, "cardId": card_id, "cacheStrategy": "DEFAULT"}
        if params_id:
            card_query["paramsId"] = params_id
        if variables:
            card_query["variables"] = variables

        _, body = _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "datatalk_query_card_analysis",
                       "arguments": {"bizId": BIZ_ID, "cardQuery": card_query,
                                     "_llmModel": _LLM_MODEL}},
        }, self.sid)
        rpc = _parse_sse(body)
        content = rpc.get("result", {}).get("content", [])
        if not content:
            self.invalidate()
            raise RuntimeError("empty content: " + str(rpc)[:300])
        text = content[0].get("text", "")
        obj = json.loads(text)
        if obj.get("code") != 0:
            self.invalidate()
            raise RuntimeError(f"beacon code={obj.get('code')} {obj.get('message')}")
        return obj.get("data", {})


def query_card(card_id, variables=None):
    """独立单次查询（每次新建 session），便于调试。"""
    return _BeaconSession().query(card_id, variables)


def _date_range_value(days):
    """生成 date_range 变量值。end 取昨天（数据完整日），start 为 end-days+1。"""
    today = datetime.datetime.now(TZ8).date()
    end = today - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days - 1)
    return f"{start.isoformat()} 00:00:00,{end.isoformat()} 23:59:59", end.isoformat()


def _clean_error_label(label):
    """去掉错误分类前缀编号，如 '19_Agent空闲超时' -> 'Agent空闲超时'。"""
    return re.sub(r"^\d+_", "", label or "")


def _top_with_other(items, top_n=10):
    """按 value 降序，保留 top_n，其余合并为'其他'。items: [{'name','value'}]"""
    if not items:
        return []
    items = sorted(items, key=lambda x: x.get("value", 0) or 0, reverse=True)
    if len(items) <= top_n:
        return items
    head = items[:top_n]
    tail = items[top_n:]
    total = sum(x.get("value", 0) or 0 for x in tail)
    if total > 0:
        head.append({"name": "其他", "value": total})
    return head


def _first_value(objects, key):
    if not objects:
        return None
    return objects[0].get(key)


# ---------------------------------------------------------------------------
# 图卡清单与字段映射
# ---------------------------------------------------------------------------
# (字段名, cardId, 取值 key, 是否支持周期切换, 口径说明)
# 图卡均来自新看板 372662；旧看板 365227 已下线。
KPI_CARDS = [
    ("new_projects", "indexCard_qgomgnqh", "project_count", True, "周期内新建项目总数"),
    ("active_projects", "indexCard_e0n3yf4d", "active_project_count", False, "周期内活跃项目数"),
    ("active_users_7d", "indexCard_gnf1t4ke", "uv", False, "协作功能使用人数"),
    ("conversation_users", "indexCard_vhesvn73", "index_0", True, "进入协作页面总人数"),
    ("node_users", "indexCard_3a6n96zj", "count", True, "工作流任务使用人数"),
    ("wecom_groups", "indexCard_jzxqo46f", "stock_count", False, "自由群聊存量"),
    ("task_created", "indexCardUltra_i9iy6r4x", "new_issue_count", True, "工作流任务新建数"),
    ("task_executed", "indexCardUltra_gl8gq211", "started_workflow_issue_count", True, "工作流任务启动数"),
]

# 趋势：字段名 -> (cardId, 日期 key, [系列 key...], 是否支持切换, 口径说明)
TREND_CARDS = {
    # 元组: (cardId, 日期key, [原始字段...], 是否切换, 口径说明, {原始字段:前端字段}, 缩放系数)
    "projects": ("line_ih7xeb30", "stat_period", ["project_count"], True, "周期内项目趋势", {}, 1),
    "active_users": ("line_m5e5qb51", "dim_0", ["index_0"], False, "进入协作页面人数", {"index_0": "active_user_count"}, 1),
    "node_users": ("line_m5e5qb51", "dim_0", ["index_0"], True, "进入协作页面人数（活跃人数趋势）", {"index_0": "active_user_count"}, 1),
    "tasks": ("line_y2b5h4x5", "stat_period", ["workflow_trigger_count", "workflow_finished_count"], True, "工作流触发/完成数",
              {"workflow_trigger_count": "new_issue_count", "workflow_finished_count": "executed_issue_count"}, 1),
    "success_rate": ("line_kr2bal8l", "stat_period", ["workflow_success_rate"], True, "任务端到端成功率",
                     {"workflow_success_rate": "success_rate"}, 100),
}

# 分布：字段名 -> (cardId, 名称 key, 值 key, 是否支持切换, 口径说明)
DIST_CARDS = {
    "engine": ("pieIndicator_r8qbk7qq", "engine", "node_count", True, "已执行工作流节点任务引擎分布"),
    "model": ("pie_zyv64x2h", "model", "node_count", True, "已执行工作流节点模型分布"),
    "fail_error": ("barStack_0soardi6", "error_category", "failure_count", False, "失败节点错误分类"),
    "task_error": ("barStack_kdrmm7x4", "error_category", "failure_count", False, "工作流任务错误原因分布"),
}


def _normalize_trend(rows, date_key, series, rename=None, scale=1):
    rename = rename or {}
    pts = []
    for r in rows:
        # 新看板图卡多为周粒度，日期键可能是 stat_period（形如 2026-11-16 ~ 2026-11-22）
        date_val = r.get(date_key) or r.get("stat_period") or r.get("dt") or r.get("stat_date")
        pt = {"date": date_val}
        for s in series:
            value = r.get(s)
            if scale != 1 and isinstance(value, (int, float)):
                value = round(value * scale, 2)
            pt[rename.get(s, s)] = value
        pts.append(pt)
    # 过滤周聚合汇总行（日期形如 "2026-11-16 ~ 2026-11-22"），只保留明细日期点；
    # 纯周粒度图卡（如项目趋势、成功率）过滤后为空，则回退保留周数据。
    daily = [p for p in pts if p.get("date") and "~" not in str(p["date"])]
    result = daily if daily else [p for p in pts if p.get("date")]
    result.sort(key=lambda p: p["date"])
    return result


def _normalize_dist(field, rows):
    card_id, name_key, value_key, _sw, _desc = DIST_CARDS[field]
    if field in ("fail_error", "task_error"):
        items = [{"name": _clean_error_label(r.get(name_key)), "value": r.get(value_key),
                  "pct": r.get("pct")} for r in rows]
        items.sort(key=lambda x: x.get("value", 0) or 0, reverse=True)
        return items
    items = [{"name": r.get(name_key), "value": r.get(value_key)} for r in rows]
    return _top_with_other(items, top_n=10)


def _fetch_with_retry(session, card_id, variables, tag, page_id=PAGE_ID, params_id=PARAMS_ID):
    """带重试的图卡查询；失败退避后重试，最多 3 次。"""
    last_err = None
    for attempt in range(3):
        time.sleep(REQUEST_INTERVAL)
        try:
            return session.query(card_id, variables, page_id=page_id,
                                 params_id=params_id).get("objects", []) or []
        except Exception as e:
            last_err = e
            # 限流时延长退避，避免雪上加霜
            time.sleep(5 * (2 ** attempt))
    print(f"[beacon] fetch failed {tag}: {last_err}", flush=True)
    return []


def build_snapshot():
    """串行 + 单 session 复用，拉取完整运营数据快照（1 天 + 7 天 + 30 天）。

    单图卡失败只记录日志、置空，不中断整体；session 失效会自动重建。
    """
    dr1, _ = _date_range_value(1)
    dr7, data_date = _date_range_value(7)
    dr30, _ = _date_range_value(30)
    var1 = [{"key": DATE_VAR, "value": dr1}]
    var7 = [{"key": DATE_VAR, "value": dr7}]
    var30 = [{"key": DATE_VAR, "value": dr30}]

    # 生成抓取任务：(tag, card_id, variables)。新看板图卡均响应 selected_date_range。
    jobs = []
    for field, card_id, _key, sw, _desc in KPI_CARDS:
        if sw:
            jobs.append((f"kpi:{field}:1d", card_id, var1))
        jobs.append((f"kpi:{field}:7d", card_id, var7))
        if sw:
            jobs.append((f"kpi:{field}:30d", card_id, var30))
    for field, (card_id, _dk, _series, sw, _desc, _rename, _scale) in TREND_CARDS.items():
        jobs.append((f"trend:{field}:7d", card_id, var7))
        if sw:
            jobs.append((f"trend:{field}:30d", card_id, var30))
    for field, (card_id, _nk, _vk, sw, _desc) in DIST_CARDS.items():
        jobs.append((f"dist:{field}:7d", card_id, var7))
        if sw:
            jobs.append((f"dist:{field}:30d", card_id, var30))

    session = _BeaconSession()
    results = {}
    for tag, card_id, var in jobs:
        results[tag] = _fetch_with_retry(session, card_id, var, tag)

    # ---- 组装 KPI ----
    kpi = {}
    definitions = {}
    for field, _card_id, key, sw, desc in KPI_CARDS:
        definitions[field] = desc
        v7 = _first_value(results.get(f"kpi:{field}:7d", []), key)
        if sw:
            v1 = _first_value(results.get(f"kpi:{field}:1d", []), key)
            v30 = _first_value(results.get(f"kpi:{field}:30d", []), key)
            kpi[field] = {"1d": v1, "7d": v7, "30d": v30}
        else:
            kpi[field] = {"7d": v7}

    # ---- 组装趋势 ----
    trends = {}
    for field, (_card_id, dk, series, sw, desc, rename, scale) in TREND_CARDS.items():
        definitions[field] = desc
        t7 = _normalize_trend(results.get(f"trend:{field}:7d", []), dk, series, rename, scale)
        if sw:
            t30 = _normalize_trend(results.get(f"trend:{field}:30d", []), dk, series, rename, scale)
            trends[field] = {"7d": t7, "30d": t30}
        else:
            trends[field] = {"7d": t7}

    # ---- 组装分布 ----
    distributions = {}
    for field, (_card_id, _nk, _vk, sw, desc) in DIST_CARDS.items():
        definitions[field] = desc
        d7 = _normalize_dist(field, results.get(f"dist:{field}:7d", []))
        if sw:
            d30 = _normalize_dist(field, results.get(f"dist:{field}:30d", []))
            distributions[field] = {"7d": d7, "30d": d30}
        else:
            distributions[field] = {"7d": d7}

    return {
        "data_date": data_date,
        "kpi": kpi,
        "trends": trends,
        "distributions": distributions,
        "definitions": definitions,
    }


# ---------------------------------------------------------------------------
# IMate 整体大盘数据（看板 326786）：用户数据 / DAU / 累计量
# ---------------------------------------------------------------------------
OVERALL_PAGE_ID = 326786


def _diff_cumulative(trend):
    """累计序列差分得到每日增量序列，日期取后一日。

    trend 形如 [{"date": "...", "value": 累计值}, ...]（按日期升序）。
    灯塔累计用户数单调递增，相邻两日差值即当日新增量。
    """
    out = []
    for i in range(1, len(trend)):
        prev = trend[i - 1].get("value")
        cur = trend[i].get("value")
        if prev is not None and cur is not None:
            out.append({"date": trend[i]["date"], "value": cur - prev})
    return out


def build_overall_snapshot():
    """抓 IMate 整体大盘数据（326786 看板），默认返回 30 天完整数据。

    该看板的日期变量是复合 JSON，且 DAU 图卡不响应简单日期过滤，
    因此直接取默认 30 天窗口，前端做 7/30 天切片展示。
    """
    session = _BeaconSession()

    dau_rows = _fetch_with_retry(session, "line_di1h5noc", None, "overall:dau",
                                 page_id=OVERALL_PAGE_ID, params_id=None)
    dau_client_rows = _fetch_with_retry(session, "line_6f4ep243", None, "overall:dau_clients",
                                        page_id=OVERALL_PAGE_ID, params_id=None)
    cum_user_rows = _fetch_with_retry(session, "line_tim1di8v", None, "overall:cum_users",
                                      page_id=OVERALL_PAGE_ID, params_id=None)
    cum_client_rows = _fetch_with_retry(session, "bar_lsfpzdwo", None, "overall:cum_clients",
                                        page_id=OVERALL_PAGE_ID, params_id=None)

    dau_trend = sorted(
        [{"date": r.get("dt"), "dau": r.get("dau"), "dau_with_chat": r.get("dau_with_chat")}
         for r in dau_rows if r.get("dt")],
        key=lambda x: x["date"],
    )
    dau_client_trend = sorted(
        [{"date": r.get("dt"), "dau": r.get("dau")}
         for r in dau_client_rows if r.get("dt")],
        key=lambda x: x["date"],
    )
    cum_user_total = sorted(
        [{"date": r.get("stat_date"), "value": r.get("cumulative_user_cnt")}
         for r in cum_user_rows if r.get("client_type") == "总计"],
        key=lambda x: x["date"],
    )
    cum_client_total = sorted(
        [{"date": r.get("stat_date"), "value": r.get("cumulative_client_cnt")}
         for r in cum_client_rows if r.get("client_type") == "总计"],
        key=lambda x: x["date"],
    )

    # 每日新增 = 相邻两日累计值差分（昨日维度的用户/客户端增量）
    daily_new_users = _diff_cumulative(cum_user_total)
    daily_new_clients = _diff_cumulative(cum_client_total)

    # 「昨日新增」必须对齐到昨天（东八区）这一完整日，而非简单取序列最后一项：
    # 手动/晚间抓取时灯塔可能已产生今天的部分数据，最后一项是「今天」，口径会错。
    yesterday = (datetime.datetime.now(TZ8).date() - datetime.timedelta(days=1)).isoformat()

    def _val_on(daily, day):
        for pt in reversed(daily):
            if pt.get("date") == day:
                return pt.get("value")
        return daily[-1]["value"] if daily else None

    kpi = {
        "total_users": cum_user_total[-1]["value"] if cum_user_total else None,
        "total_clients": cum_client_total[-1]["value"] if cum_client_total else None,
        "dau": dau_trend[-1]["dau"] if dau_trend else None,
        "dau_clients": dau_client_trend[-1]["dau"] if dau_client_trend else None,
        # 昨日维度：昨天这一天新增的用户 / 客户端
        "new_users": _val_on(daily_new_users, yesterday),
        "new_clients": _val_on(daily_new_clients, yesterday),
    }

    return {
        "data_date": yesterday,
        "kpi": kpi,
        "trends": {
            "dau": dau_trend,
            "dau_clients": dau_client_trend,
            "cum_users": cum_user_total,
            "cum_clients": cum_client_total,
            "new_users": daily_new_users,
            "new_clients": daily_new_clients,
        },
    }
