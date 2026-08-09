/**
 * AI 视频工厂 · 云端控制面（Cloudflare Worker）
 *
 * 只做三件事：收订单、给本地渲染代理派活、存产物。
 * 解析脚本与工具选型的逻辑在本地 Python（studio/intake.py），不在这里重写
 * ——两套实现必然漂移，代理认领订单后自己规划再把计划回传即可。
 *
 * 绑定：DB (D1) / ASSETS_BUCKET (R2) / ASSETS (静态资源)
 * 密钥：APP_TOKEN（界面与代理共用）
 */

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });

const now = () => Date.now();
const uid = () => crypto.randomUUID().replace(/-/g, '').slice(0, 16);

function authed(request, env) {
  const token = env.APP_TOKEN;
  if (!token) return true; // 未设置密钥时不拦截（仅本地调试）
  const header = request.headers.get('authorization') || '';
  const bearer = header.startsWith('Bearer ') ? header.slice(7) : '';
  const url = new URL(request.url);
  return bearer === token || url.searchParams.get('token') === token;
}

async function readJson(request) {
  try { return await request.json(); } catch { return {}; }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // ---------- 静态界面 ----------
    if (!path.startsWith('/api/') && !path.startsWith('/media/')) {
      return env.ASSETS.fetch(request);
    }

    // ---------- 产物读取 ----------
    if (path.startsWith('/media/')) {
      if (!authed(request, env)) return json({ error: '未授权' }, 401);
      const key = decodeURIComponent(path.slice('/media/'.length));
      const obj = await env.ASSETS_BUCKET.get(key);
      if (!obj) return json({ error: '产物不存在' }, 404);
      const headers = new Headers();
      obj.writeHttpMetadata(headers);
      headers.set('etag', obj.httpEtag);
      headers.set('cache-control', 'public, max-age=31536000, immutable');
      return new Response(obj.body, { headers });
    }

    if (!authed(request, env)) return json({ error: '未授权：请提供访问令牌' }, 401);

    try {
      // ---------- 健康检查 ----------
      if (path === '/api/health') {
        return json({ ok: true, time: now(), has_token: Boolean(env.APP_TOKEN) });
      }

      // ---------- 下单 ----------
      if (path === '/api/orders' && method === 'POST') {
        const body = await readJson(request);
        const brief = (body.brief || '').trim();
        if (!brief) return json({ error: '内容为空' }, 400);
        const id = uid();
        const t = now();
        await env.DB.prepare(
          `INSERT INTO orders (id,title,brief,status,plan_json,note,agent,created_at,updated_at)
           VALUES (?,?,?,'pending','','','',?,?)`
        ).bind(id, (body.title || '').slice(0, 200), brief, t, t).run();

        // 叫醒云端渲染容器。它是按需唤醒的（常驻会一直计费），所以下单后
        // 必须主动踢一脚，否则订单会一直挂着等本地代理。
        // 唤醒失败不影响下单 —— 本地代理在跑的话照样能接。
        if (env.RENDERER_WAKE_URL) {
          try {
            await fetch(env.RENDERER_WAKE_URL + '/wake', {
              method: 'POST',
              headers: { authorization: `Bearer ${env.APP_TOKEN || ''}` },
            });
          } catch (_) { /* 容器唤醒失败不阻塞下单 */ }
        }
        return json({ id, status: 'pending' });
      }

      // ---------- 订单列表 ----------
      if (path === '/api/orders' && method === 'GET') {
        const { results: orders } = await env.DB.prepare(
          `SELECT id,title,status,note,agent,created_at,updated_at,
                  substr(brief,1,160) AS brief_preview, plan_json
           FROM orders ORDER BY created_at DESC LIMIT 50`
        ).all();
        const { results: jobs } = await env.DB.prepare(
          `SELECT id,order_id,tool,tool_label,label,status,artifacts,error,cost_usd,elapsed,updated_at
           FROM jobs ORDER BY created_at ASC LIMIT 500`
        ).all();
        const byOrder = {};
        for (const j of jobs) {
          j.artifacts = JSON.parse(j.artifacts || '[]');
          (byOrder[j.order_id] ||= []).push(j);
        }
        for (const o of orders) {
          o.jobs = byOrder[o.id] || [];
          o.plan = o.plan_json ? JSON.parse(o.plan_json) : null;
          delete o.plan_json;
        }
        return json({ orders });
      }

      // ---------- 单个订单 ----------
      const orderMatch = path.match(/^\/api\/orders\/([a-z0-9]+)$/i);
      if (orderMatch && method === 'GET') {
        const o = await env.DB.prepare('SELECT * FROM orders WHERE id=?')
          .bind(orderMatch[1]).first();
        if (!o) return json({ error: '订单不存在' }, 404);
        const { results: jobs } = await env.DB.prepare(
          'SELECT * FROM jobs WHERE order_id=? ORDER BY created_at ASC'
        ).bind(o.id).all();
        for (const j of jobs) {
          j.artifacts = JSON.parse(j.artifacts || '[]');
          j.inputs = JSON.parse(j.inputs_json || '{}');
          delete j.inputs_json;
        }
        o.plan = o.plan_json ? JSON.parse(o.plan_json) : null;
        delete o.plan_json;
        return json({ order: o, jobs });
      }

      if (orderMatch && method === 'DELETE') {
        await env.DB.prepare('DELETE FROM jobs WHERE order_id=?').bind(orderMatch[1]).run();
        await env.DB.prepare('DELETE FROM orders WHERE id=?').bind(orderMatch[1]).run();
        return json({ deleted: true });
      }

      // ---------- 代理：认领待办订单 ----------
      if (path === '/api/agent/claim' && method === 'POST') {
        const body = await readJson(request);
        const agent = (body.agent || 'local').slice(0, 60);
        const o = await env.DB.prepare(
          `SELECT id,title,brief FROM orders WHERE status='pending'
           ORDER BY created_at ASC LIMIT 1`
        ).first();
        if (!o) return json({ order: null });
        await env.DB.prepare(
          `UPDATE orders SET status='claimed', agent=?, updated_at=? WHERE id=? AND status='pending'`
        ).bind(agent, now(), o.id).run();
        return json({ order: o });
      }

      // ---------- 代理：回传计划 ----------
      if (path === '/api/agent/plan' && method === 'POST') {
        const body = await readJson(request);
        if (!body.order_id) return json({ error: '缺少 order_id' }, 400);
        await env.DB.prepare(
          `UPDATE orders SET plan_json=?, status=?, note=?, updated_at=? WHERE id=?`
        ).bind(
          JSON.stringify(body.plan || {}),
          body.status || 'planned',
          (body.note || '').slice(0, 500),
          now(), body.order_id
        ).run();
        return json({ ok: true });
      }

      // ---------- 代理：上报任务 ----------
      if (path === '/api/agent/job' && method === 'POST') {
        const b = await readJson(request);
        if (!b.order_id || !b.id) return json({ error: '缺少 order_id 或 id' }, 400);
        const t = now();
        await env.DB.prepare(
          `INSERT INTO jobs (id,order_id,tool,tool_label,label,status,inputs_json,
                             artifacts,error,cost_usd,elapsed,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             status=excluded.status, artifacts=excluded.artifacts, error=excluded.error,
             cost_usd=excluded.cost_usd, elapsed=excluded.elapsed, updated_at=excluded.updated_at`
        ).bind(
          b.id, b.order_id, b.tool || '', b.tool_label || '', b.label || '',
          b.status || 'queued', JSON.stringify(b.inputs || {}),
          JSON.stringify(b.artifacts || []), (b.error || '').slice(0, 2000),
          Number(b.cost_usd || 0), Number(b.elapsed || 0), t, t
        ).run();
        return json({ ok: true });
      }

      // ---------- 代理：订单收尾 ----------
      if (path === '/api/agent/finish' && method === 'POST') {
        const b = await readJson(request);
        await env.DB.prepare('UPDATE orders SET status=?, note=?, updated_at=? WHERE id=?')
          .bind(b.status || 'done', (b.note || '').slice(0, 500), now(), b.order_id).run();
        return json({ ok: true });
      }

      // ---------- 代理：上传产物 ----------
      if (path.startsWith('/api/agent/upload/') && method === 'PUT') {
        const key = decodeURIComponent(path.slice('/api/agent/upload/'.length));
        if (!key || key.includes('..')) return json({ error: '非法的对象名' }, 400);
        await env.ASSETS_BUCKET.put(key, request.body, {
          httpMetadata: {
            contentType: request.headers.get('content-type') || 'application/octet-stream',
          },
        });
        return json({ ok: true, key });
      }

      // ---------- 成片库 ----------
      if (path === '/api/assets' && method === 'GET') {
        const list = await env.ASSETS_BUCKET.list({ limit: 200 });
        return json({
          objects: list.objects.map(o => ({
            key: o.key, size: o.size, uploaded: o.uploaded,
          })).sort((a, b) => new Date(b.uploaded) - new Date(a.uploaded)),
        });
      }

      return json({ error: '未知接口' }, 404);
    } catch (err) {
      return json({ error: String(err && err.message || err) }, 500);
    }
  },
};
