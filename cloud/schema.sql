-- AI 视频工厂 · 云端控制面
-- 订单：一次派单（一段脚本 / 一个文档）
CREATE TABLE IF NOT EXISTS orders (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  brief       TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending/claimed/planned/running/done/failed
  plan_json   TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT '',
  agent       TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- 任务：订单展开出来的具体工具调用
CREATE TABLE IF NOT EXISTS jobs (
  id           TEXT PRIMARY KEY,
  order_id     TEXT NOT NULL,
  tool         TEXT NOT NULL,
  tool_label   TEXT NOT NULL DEFAULT '',
  label        TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'queued',  -- queued/running/success/failed
  inputs_json  TEXT NOT NULL DEFAULT '{}',
  artifacts    TEXT NOT NULL DEFAULT '[]',      -- R2 key 数组
  error        TEXT NOT NULL DEFAULT '',
  cost_usd     REAL NOT NULL DEFAULT 0,
  elapsed      REAL NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_order ON jobs(order_id, created_at);
