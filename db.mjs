/**
 * PostgreSQL 连接池（IMate 业务库，只读）。
 *
 * 凭据一律从环境变量读取，绝不硬编码、绝不提交进仓库。
 * 本地开发把真实值写进项目根目录的 .env（已被 .gitignore 忽略）。
 */
import pg from 'pg';

const { Pool } = pg;

/** 必填的环境变量键名 */
export const REQUIRED_ENV_KEYS = [
  'PGHOST',
  'PGPORT',
  'PGUSER',
  'PGPASSWORD',
  'PGDATABASE',
];

/** 返回缺失的环境变量键名列表 */
export function missingEnvKeys() {
  return REQUIRED_ENV_KEYS.filter((k) => !process.env[k]);
}

/** 数据库是否已完成配置 */
export function isDbConfigured() {
  return missingEnvKeys().length === 0;
}

let pool = null;

/**
 * 懒加载连接池：只有在环境变量齐备时才真正创建，
 * 这样未配置 .env 也不会导致服务启动失败。
 */
export function getPool() {
  if (!isDbConfigured()) return null;
  if (pool) return pool;

  pool = new Pool({
    host: process.env.PGHOST,
    port: Number(process.env.PGPORT),
    user: process.env.PGUSER,
    password: process.env.PGPASSWORD,
    database: process.env.PGDATABASE,
    options: process.env.PGSCHEMA ? `-c search_path=${process.env.PGSCHEMA}` : undefined,
    max: Number(process.env.PGPOOL_MAX ?? 10),
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 8_000,
    ssl: process.env.PGSSL === 'true' ? { rejectUnauthorized: false } : undefined,
  });

  pool.on('error', (err) => {
    console.error('[db] 空闲连接异常：', err.message);
  });

  return pool;
}

/** 执行查询；未配置数据库时抛出可识别错误 */
export async function query(text, params) {
  const p = getPool();
  if (!p) {
    const err = new Error('数据库未配置：请先在 .env 中填入 PG* 变量');
    err.code = 'DB_NOT_CONFIGURED';
    throw err;
  }
  return p.query(text, params);
}

/** 连通性探测：成功返回服务端时间 */
export async function ping() {
  const res = await query('select now() as now');
  return res.rows[0]?.now ?? null;
}

/** 优雅关闭 */
export async function closePool() {
  if (pool) {
    await pool.end();
    pool = null;
  }
}
