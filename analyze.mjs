/**
 * IMate 业务库（PostgreSQL 只读）接入示例 / 数据分析脚本。
 *
 * 运行：node --env-file=.env analyze.mjs
 *
 * 用途：仅做数据分析，不涉及报名 / 点赞数据。
 * 凭据来自 .env（已被 .gitignore 忽略），SQL 全部使用 $1/$2 参数化占位。
 */
import { query, ping, closePool, isDbConfigured } from './db.mjs';

/** 与参考项目 server/scoring.mjs 保持一致的只读统计 SQL（原样复制）。 */
const SCORE_SQL = `
  WITH ids AS (SELECT unnest($1::uuid[]) AS project_id),
  q AS MATERIALIZED (
    SELECT i.project_id, q.issue_id, q.agent_id, q.status, q.completed_at
    FROM agent_task_queue q JOIN issue i ON i.id = q.issue_id JOIN ids ON ids.project_id = i.project_id
    WHERE i.deleted_at IS NULL
  ),
  d_free AS (SELECT s.project_id, (s.created_at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS n
    FROM chat_session s JOIN ids USING (project_id)
    WHERE s.chat_type = 'free' AND s.archived_at IS NULL AND s.created_at >= $2 AND s.created_at <= $3
      AND EXISTS (SELECT 1 FROM chat_message m WHERE m.chat_session_id = s.id AND m.sender_type = 'user')
    GROUP BY 1, 2),
  wf_done AS (
    SELECT i.project_id, COALESCE(
        (SELECT MAX(l.created_at) FROM activity_log l WHERE l.issue_id = i.id AND l.action = 'status_changed' AND l.details->>'to' = 'completed'),
        (SELECT MAX(w.completed_at) FROM workflow w WHERE w.issue_id = i.id), i.closed_at) AS at
    FROM issue i JOIN ids USING (project_id)
    WHERE i.status = 'completed' AND EXISTS (SELECT 1 FROM workflow w WHERE w.issue_id = i.id)
  ),
  d_node AS (SELECT project_id, (at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS n
    FROM wf_done WHERE at >= $2 AND at <= $3 GROUP BY 1, 2),
  tapd_done AS (
    SELECT i.project_id, COALESCE(
        (SELECT MAX(l.created_at) FROM activity_log l WHERE l.issue_id = i.id AND l.action = 'status_changed' AND l.details->>'to' = 'completed'),
        (SELECT MAX(w.completed_at) FROM workflow w WHERE w.issue_id = i.id), i.closed_at, i.updated_at) AS at
    FROM issue i JOIN ids USING (project_id)
    WHERE i.status = 'completed' AND i.origin_type = 'tapd'
      AND NULLIF(btrim(i.title), '') IS NOT NULL
  ),
  d_tapd AS (SELECT project_id, (at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS n
    FROM tapd_done WHERE at >= $2 AND at <= $3 GROUP BY 1, 2),
  d_run AS (SELECT w.project_id, (COALESCE(w.started_at, w.created_at) AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS n
    FROM workflow w JOIN ids USING (project_id)
    WHERE w.template_id IS NOT NULL AND w.status NOT IN ('draft', 'planning')
      AND COALESCE(w.started_at, w.created_at) >= $2 AND COALESCE(w.started_at, w.created_at) <= $3 GROUP BY 1, 2),
  hum AS (
    SELECT i.project_id, i.creator_id AS uid, i.created_at AS at FROM issue i JOIN ids USING (project_id)
    WHERE i.creator_type = 'member' AND i.creator_id IS NOT NULL AND i.deleted_at IS NULL AND i.created_at >= $2 AND i.created_at <= $3
    UNION ALL
    SELECT c.project_id, c.author_id, c.created_at FROM comment c JOIN ids USING (project_id)
    WHERE c.author_type = 'member' AND c.author_id IS NOT NULL AND c.created_at >= $2 AND c.created_at <= $3
    UNION ALL
    SELECT l.project_id, l.actor_id, l.created_at FROM activity_log l JOIN ids USING (project_id)
    WHERE l.actor_type = 'member' AND l.actor_id IS NOT NULL AND l.created_at >= $2 AND l.created_at <= $3
  ),
  d_hum AS (SELECT project_id, (at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(DISTINCT uid) AS n FROM hum GROUP BY 1, 2),
  ag AS (
    SELECT project_id, agent_id AS aid, completed_at AS at FROM q
    WHERE status = 'completed' AND agent_id IS NOT NULL AND completed_at >= $2 AND completed_at <= $3
    UNION ALL
    SELECT w.project_id, n.agent_id, n.completed_at FROM workflow_node n JOIN workflow w ON w.id = n.workflow_id JOIN ids USING (project_id)
    WHERE n.status = 'completed' AND n.agent_id IS NOT NULL AND n.completed_at >= $2 AND n.completed_at <= $3
    UNION ALL
    SELECT l.project_id, l.actor_id, l.created_at FROM activity_log l JOIN ids USING (project_id)
    WHERE l.actor_type = 'agent' AND l.actor_id IS NOT NULL AND l.created_at >= $2 AND l.created_at <= $3
  ),
  d_ag AS (SELECT project_id, (at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(DISTINCT aid) AS n FROM ag GROUP BY 1, 2),
  days AS (SELECT project_id, day FROM d_free UNION SELECT project_id, day FROM d_node UNION SELECT project_id, day FROM d_run
    UNION SELECT project_id, day FROM d_tapd UNION SELECT project_id, day FROM d_hum UNION SELECT project_id, day FROM d_ag),
  daily AS (
    SELECT d.project_id, json_agg(json_build_object('day', d.day, 'freeTasks', COALESCE(f.n, 0), 'workflowTasks', COALESCE(nd.n, 0),
      'runs', COALESCE(r.n, 0), 'tapd', COALESCE(t.n, 0), 'humanActive', COALESCE(h.n, 0), 'agentActive', COALESCE(a.n, 0)) ORDER BY d.day) AS days
    FROM days d LEFT JOIN d_free f USING (project_id, day) LEFT JOIN d_node nd USING (project_id, day)
    LEFT JOIN d_run r USING (project_id, day) LEFT JOIN d_tapd t USING (project_id, day) LEFT JOIN d_hum h USING (project_id, day) LEFT JOIN d_ag a USING (project_id, day)
    GROUP BY d.project_id
  ),
  t_hum AS (SELECT m.project_id, COUNT(DISTINCT m.user_id) AS n FROM project_member m JOIN ids USING (project_id)
    WHERE m.deleted_at IS NULL GROUP BY 1),
  t_ag AS (SELECT pa.project_id, COUNT(DISTINCT pa.agent_id) AS n FROM project_agent pa JOIN ids USING (project_id)
    JOIN agent a ON a.id = pa.agent_id WHERE a.archived_at IS NULL GROUP BY 1),
  t_tpl AS (SELECT t.project_id, COUNT(*) AS n FROM workflow_template t JOIN ids USING (project_id)
    WHERE t.archived_at IS NULL
      AND EXISTS (SELECT 1 FROM workflow w WHERE w.template_id = t.id AND w.status NOT IN ('draft', 'planning'))
    GROUP BY 1)
  SELECT p.id AS project_id, p.name, COALESCE(th.n, 0) AS humans, COALESCE(ta.n, 0) AS agents, COALESCE(tt.n, 0) AS templates,
    COALESCE(dl.days, '[]'::json) AS days
  FROM project p JOIN ids ON ids.project_id = p.id
  LEFT JOIN t_hum th ON th.project_id = p.id LEFT JOIN t_ag ta ON ta.project_id = p.id
  LEFT JOIN t_tpl tt ON tt.project_id = p.id LEFT JOIN daily dl ON dl.project_id = p.id
  WHERE p.deleted_at IS NULL`;

async function main() {
  if (!isDbConfigured()) {
    console.error('数据库未配置：请在 .env 中填入 PG* 变量');
    process.exitCode = 1;
    return;
  }

  console.log('=== 1) 连通性探测 ping() ===');
  const serverTime = await ping();
  console.log('数据库服务端时间:', serverTime);

  console.log('\n=== 2) 只读查询：SELECT id, name FROM project ... ===');
  const { rows: projects } = await query(
    'SELECT id, name FROM project WHERE deleted_at IS NULL LIMIT 5'
  );
  console.log('项目数据:', JSON.stringify(projects, null, 2));

  console.log('\n=== 3) 示例：按活动期统计项目关键指标（SCORE_SQL） ===');
  const projectIds = projects.map((p) => p.id);
  if (projectIds.length === 0) {
    console.log('没有可用项目，跳过活动期统计示例。');
    return;
  }
  const startTime = '2026-01-01T00:00:00+08:00';
  const endTime = '2026-12-31T23:59:59+08:00';
  const { rows: scoreRows } = await query(SCORE_SQL, [projectIds, startTime, endTime]);
  console.log('活动期统计（原始逐日数据）:');
  for (const r of scoreRows) {
    const days = Array.isArray(r.days) ? r.days : [];
    console.log(`- ${r.name} (${r.project_id})`);
    console.log(`  成员 ${r.humans} / Agent ${r.agents} / 模板 ${r.templates}，有数据的天数 ${days.length}`);
    if (days.length) console.log('  逐日样例:', JSON.stringify(days.slice(0, 3)));
  }
}

main()
  .catch((err) => {
    console.error('分析脚本执行失败:', err);
    process.exitCode = 1;
  })
  .finally(async () => {
    await closePool();
  });
