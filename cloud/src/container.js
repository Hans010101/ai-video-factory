/**
 * 渲染容器的 Worker 入口。
 *
 * Cloudflare Containers 由 Durable Object 托管，必须有请求触发才会唤醒。
 * 渲染任务本身是「拉」模式（容器主动轮询控制面认领订单），所以这里只需要
 * 两件事：把容器叫醒、让它在空闲后自己睡下去。
 *
 * 为什么不常驻：内存与磁盘按预置量计费，8GB 容器挂满一个月约 $55；
 * 按需唤醒后每月只有真正渲染的那几十分钟计费，约 $5–15。
 */

import { Container, getContainer } from '@cloudflare/containers';

export class RendererContainer extends Container {
  // 渲染一条片子通常 1–3 分钟。给足空闲窗口，避免连续下单时反复冷启动
  // （冷启动要拉起 Python + Node + Chromium，代价不小）。
  sleepAfter = '10m';

  // 密钥不写进镜像，运行时从 Worker Secrets 注入
  envVars = {
    AVF_TOKEN: this.env.APP_TOKEN ?? '',
    ELEVENLABS_API_KEY: this.env.ELEVENLABS_API_KEY ?? '',
    GOOGLE_API_KEY: this.env.GOOGLE_API_KEY ?? '',
    OPENAI_API_KEY: this.env.OPENAI_API_KEY ?? '',
    PEXELS_API_KEY: this.env.PEXELS_API_KEY ?? '',
  };

  onStart() {
    console.log('渲染容器已启动，开始轮询订单');
  }

  onStop(reason) {
    console.log('渲染容器已停止:', reason?.exitCode ?? '', reason?.reason ?? '');
  }

  onError(err) {
    console.error('渲染容器异常:', err);
  }
}

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });

function authed(request, env) {
  if (!env.APP_TOKEN) return true;
  const h = request.headers.get('authorization') || '';
  const url = new URL(request.url);
  return h.slice(7) === env.APP_TOKEN || url.searchParams.get('token') === env.APP_TOKEN;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!authed(request, env)) return json({ error: '未授权' }, 401);

    // 唤醒：控制面收到订单后调这里，容器起来后自己去认领
    if (url.pathname === '/wake') {
      const c = getContainer(env.RENDERER);
      try {
        await c.startAndWaitForPorts({ startOptions: { envVars: {} } });
        return json({ ok: true, state: 'running' });
      } catch (err) {
        // 容器没有对外端口（它只发出站请求），startAndWaitForPorts 会超时，
        // 但那时进程其实已经跑起来了 —— 这种情况不算失败。
        return json({ ok: true, state: 'started', note: String(err).slice(0, 120) });
      }
    }

    if (url.pathname === '/status') {
      const c = getContainer(env.RENDERER);
      return json({ alive: Boolean(c) });
    }

    return json({
      service: 'ai-video-factory-renderer',
      usage: 'POST /wake 唤醒渲染容器；容器会自行轮询控制面认领订单',
    });
  },
};
