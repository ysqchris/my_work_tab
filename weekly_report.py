"""协作数据周报生成器：从运营库 PostgreSQL（multica）深挖协作数据。

输出结构化 JSON，供前端运营页「周报」板块可视化展示。
三个方向：协作活跃度与趋势 / 用户与功能使用分析 / 任务与Agent执行质量。

周期口径：最近一个完整自然周（周一 00:00 ~ 周日 23:59，东八区），
并与上一周做环比。连接信息只从环境变量读取（PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE）。
"""
import datetime
import json
import os

import psycopg2
import psycopg2.extras

TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and not os.environ.get(key, "").strip():
                os.environ[key] = value


_load_env()


def _connect():
    return psycopg2.connect(
        host=os.environ["PGHOST"].strip(),
        port=int(os.environ.get("PGPORT", "5432") or 5432),
        user=os.environ["PGUSER"].strip(),
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"].strip(),
        connect_timeout=15,
    )


def _periods(today=None):
    """返回 (完整周周一, 完整周周日, 上一周周一, 上一周周日)，均为东八区已结束的自然周。"""
    today = today or datetime.datetime.now(TZ8).date()
    current_monday = today - datetime.timedelta(days=today.weekday())
    # 最近一个已结束的完整周 = 当前自然周的前一周
    this_monday = current_monday - datetime.timedelta(days=7)
    this_sunday = this_monday + datetime.timedelta(days=6)
    prev_monday = this_monday - datetime.timedelta(days=7)
    prev_sunday = this_sunday - datetime.timedelta(days=7)
    return this_monday, this_sunday, prev_monday, prev_sunday


def _rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return [dict(r) for r in cur.fetchall()]


def _one(cur, sql, params=None, default=0):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    if not row:
        return default
    if isinstance(row, dict):
        value = next(iter(row.values()))
    else:
        value = row[0]
    return default if value is None else value


def _pct_change(cur_val, prev_val):
    if prev_val in (None, 0):
        return None
    return round((cur_val - prev_val) / prev_val * 100, 1)


def build_report():
    mon, sun, pmon, psun = _periods()
    start, end = mon.isoformat(), (sun + datetime.timedelta(days=1)).isoformat()
    pstart, pend = pmon.isoformat(), (psun + datetime.timedelta(days=1)).isoformat()

    report = {
        "period": {"start": mon.isoformat(), "end": sun.isoformat(),
                   "label": f"{mon.isoformat()} ~ {sun.isoformat()}"},
        "prev_period": {"start": pmon.isoformat(), "end": psun.isoformat()},
        "generated_at": datetime.datetime.now(TZ8).isoformat(timespec="seconds"),
        "activity": {},
        "users": {},
        "quality": {},
    }

    conn = _connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # ============ 方向1：协作活跃度与趋势 ============
        def count_between(table, where_extra="", col="created_at", soft_delete=False):
            del_sql = " AND deleted_at IS NULL" if soft_delete else ""
            sql = (f"SELECT count(*) FROM public.{table} WHERE {col} >= %s AND {col} < %s"
                   + del_sql + where_extra)
            return {
                "current": _one(cur, sql, (start, end)),
                "previous": _one(cur, sql, (pstart, pend)),
            }

        kpis = {
            "new_projects": count_between("project", soft_delete=True),
            "new_issues": count_between("issue", soft_delete=True),
            "new_sessions": count_between("chat_session"),
            "messages": count_between("chat_message"),
        }
        # 活跃用户数（有消息行为的用户）
        active_user_sql = ("SELECT count(DISTINCT sender_id) FROM public.chat_message "
                           "WHERE sender_type='user' AND created_at >= %s AND created_at < %s")
        kpis["active_users"] = {
            "current": _one(cur, active_user_sql, (start, end)),
            "previous": _one(cur, active_user_sql, (pstart, pend)),
        }
        for key, val in kpis.items():
            val["change_pct"] = _pct_change(val["current"], val["previous"])
        report["activity"]["kpis"] = kpis

        # 每日趋势（本周）
        trend_sqls = {
            "projects": ("SELECT date_trunc('day', created_at AT TIME ZONE 'Asia/Shanghai')::date::text AS d, "
                         "count(*) n FROM public.project WHERE created_at>=%s AND created_at<%s AND deleted_at IS NULL "
                         "GROUP BY 1 ORDER BY 1", (start, end)),
            "issues": ("SELECT date_trunc('day', created_at AT TIME ZONE 'Asia/Shanghai')::date::text AS d, "
                       "count(*) n FROM public.issue WHERE created_at>=%s AND created_at<%s AND deleted_at IS NULL "
                       "GROUP BY 1 ORDER BY 1", (start, end)),
            "sessions": ("SELECT date_trunc('day', created_at AT TIME ZONE 'Asia/Shanghai')::date::text AS d, "
                         "count(*) n FROM public.chat_session WHERE created_at>=%s AND created_at<%s "
                         "GROUP BY 1 ORDER BY 1", (start, end)),
            "messages": ("SELECT date_trunc('day', created_at AT TIME ZONE 'Asia/Shanghai')::date::text AS d, "
                         "count(*) n FROM public.chat_message WHERE created_at>=%s AND created_at<%s "
                         "GROUP BY 1 ORDER BY 1", (start, end)),
        }
        trends = {}
        for name, (sql, params) in trend_sqls.items():
            trends[name] = [{"date": r["d"], "value": r["n"]} for r in _rows(cur, sql, params)]
        report["activity"]["trends"] = trends

        # 活跃项目 Top10（按本周消息数）
        top_projects_sql = """
            SELECT p.name AS name, count(*)::int AS messages,
                   count(DISTINCT m.sender_id) FILTER (WHERE m.sender_type='user')::int AS users
            FROM public.chat_message m
            JOIN public.chat_session s ON s.id = m.chat_session_id
            JOIN public.project p ON p.id = s.project_id
            WHERE m.created_at>=%s AND m.created_at<%s AND p.deleted_at IS NULL
            GROUP BY p.id, p.name ORDER BY messages DESC LIMIT 10
        """
        report["activity"]["top_projects"] = _rows(cur, top_projects_sql, (start, end))

        # ============ 方向2：用户与功能使用分析 ============
        # 新增用户
        new_user_sql = "SELECT count(*) FROM public.\"user\" WHERE created_at>=%s AND created_at<%s"
        report["users"]["new_users"] = {
            "current": _one(cur, new_user_sql, (start, end)),
            "previous": _one(cur, new_user_sql, (pstart, pend)),
        }
        # 留存：本周活跃且上周也活跃的用户占比
        retained_sql = """
            SELECT count(*) FROM (
                SELECT DISTINCT sender_id FROM public.chat_message
                WHERE sender_type='user' AND created_at>=%s AND created_at<%s
                INTERSECT
                SELECT DISTINCT sender_id FROM public.chat_message
                WHERE sender_type='user' AND created_at>=%s AND created_at<%s
            ) t
        """
        retained = _one(cur, retained_sql, (start, end, pstart, pend))
        report["users"]["retained_users"] = retained
        report["users"]["retention_pct"] = (
            round(retained / kpis["active_users"]["current"] * 100, 1)
            if kpis["active_users"]["current"] else None
        )

        # 功能使用分布（会话类型）
        chat_type_sql = """
            SELECT chat_type AS name, count(*)::int AS sessions
            FROM public.chat_session WHERE created_at>=%s AND created_at<%s
            GROUP BY 1 ORDER BY 2 DESC
        """
        chat_types = _rows(cur, chat_type_sql, (start, end))
        type_labels = {"issue": "任务会话", "node": "节点会话", "free": "自由群聊",
                       "template": "模板会话", "direct": "直聊"}
        for row in chat_types:
            row["label"] = type_labels.get(row["name"], row["name"] or "其他")
        report["users"]["chat_types"] = chat_types

        # 高价值用户 Top10（按本周发送消息数）
        top_users_sql = """
            SELECT u.name AS name, u.nick AS nick, count(*)::int AS messages,
                   count(DISTINCT m.chat_session_id)::int AS sessions
            FROM public.chat_message m
            JOIN public."user" u ON u.id = m.sender_id
            WHERE m.sender_type='user' AND m.created_at>=%s AND m.created_at<%s
            GROUP BY u.id, u.name, u.nick ORDER BY messages DESC LIMIT 10
        """
        report["users"]["top_users"] = _rows(cur, top_users_sql, (start, end))

        # ============ 方向3：任务与Agent执行质量 ============
        # 节点执行状态
        node_status_sql = """
            SELECT status AS name, count(*)::int AS value
            FROM public.workflow_node WHERE created_at>=%s AND created_at<%s
            GROUP BY 1 ORDER BY 2 DESC
        """
        statuses = _rows(cur, node_status_sql, (start, end))
        report["quality"]["node_status"] = statuses
        done = sum(r["value"] for r in statuses if r["name"] in ("completed", "failed", "skipped", "cancelled"))
        failed = sum(r["value"] for r in statuses if r["name"] == "failed")
        report["quality"]["node_success_rate"] = round((done - failed) / done * 100, 1) if done else None
        report["quality"]["node_total"] = done

        # 失败原因分布
        fail_sql = """
            SELECT failure_reason AS name, count(*)::int AS value
            FROM public.workflow_node
            WHERE created_at>=%s AND created_at<%s AND failure_reason IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12
        """
        report["quality"]["failure_reasons"] = _rows(cur, fail_sql, (start, end))

        # Agent 引擎分布（按执行统计的 turn 数）
        engine_sql = """
            SELECT agent_kind AS name, count(*)::int AS tasks,
                   sum(turn_count)::int AS turns, sum(tool_count)::int AS tools
            FROM public.agent_task_execution_stat
            WHERE created_at>=%s AND created_at<%s
            GROUP BY 1 ORDER BY tasks DESC
        """
        report["quality"]["engines"] = _rows(cur, engine_sql, (start, end))

        # 消息响应耗时（P50 / P90 / 平均，秒）
        latency_sql = """
            SELECT round(avg(elapsed_ms)/1000.0)::int AS avg_s,
                   round(percentile_cont(0.5) within group(order by elapsed_ms)/1000.0)::int AS p50_s,
                   round(percentile_cont(0.9) within group(order by elapsed_ms)/1000.0)::int AS p90_s,
                   count(*)::int AS n
            FROM public.chat_message
            WHERE created_at>=%s AND created_at<%s AND role='assistant'
              AND status='complete' AND elapsed_ms>0
        """
        row = _rows(cur, latency_sql, (start, end))
        report["quality"]["latency"] = row[0] if row else {}

        cur.close()
    finally:
        conn.close()

    return report


if __name__ == "__main__":
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
