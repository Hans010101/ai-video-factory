"""本地渲染代理：认领云端订单，用本机算力执行，产物回传 R2。

架构上这是「控制面上云、算力留本地」的那条腿：
    云端 Worker（免费额度）负责收单、存任务记录与产物
    本机负责真正的渲染 —— FFmpeg / Remotion / Piper / 102 个工具

只发起出站 HTTPS 请求，不监听端口，所以不用做端口转发或内网穿透。

    .venv/bin/python -m studio.agent --url https://xxx.workers.dev --token <令牌>
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
POLL_IDLE = 6.0     # 无单时的轮询间隔（秒）
POLL_BUSY = 1.0


class Cloud:
    """云端控制面的最小客户端。"""

    def __init__(self, base_url: str, token: str, agent: str) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.agent = agent

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str = "application/json") -> dict[str, Any]:
        req = urllib.request.Request(f"{self.base}{path}", data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        # Cloudflare 的机器人防护会对默认的 Python-urllib UA 直接返回 403，
        # 必须显式声明一个正常的 User-Agent。
        req.add_header("User-Agent", f"AIVideoFactory-Agent/1.0 ({self.agent})")
        if body is not None:
            req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        if not raw:
            return {}
        import json
        return json.loads(raw)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json
        return self._request("POST", path, json.dumps(payload, ensure_ascii=False).encode())

    def claim(self) -> Optional[dict[str, Any]]:
        return self.post("/api/agent/claim", {"agent": self.agent}).get("order")

    def send_plan(self, order_id: str, plan: dict[str, Any], status: str, note: str) -> None:
        self.post("/api/agent/plan", {"order_id": order_id, "plan": plan,
                                      "status": status, "note": note})

    def send_job(self, payload: dict[str, Any]) -> None:
        self.post("/api/agent/job", payload)

    def finish(self, order_id: str, status: str, note: str = "") -> None:
        self.post("/api/agent/finish", {"order_id": order_id, "status": status, "note": note})

    def upload(self, key: str, path: Path) -> str:
        mime, _ = mimetypes.guess_type(str(path))
        data = path.read_bytes()
        self._request("PUT", f"/api/agent/upload/{urllib.parse.quote(key)}", data,
                      mime or "application/octet-stream")
        return key


def _prepare_env() -> None:
    """和 studio/__main__.py 相同的 PATH / TLS 修复，代理独立启动时也要生效。"""
    extra = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join([p for p in extra if p and p not in current] + current)

    import ssl
    if not ssl.get_default_verify_paths().cafile:
        try:
            import certifi
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
            os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
        except ImportError:
            pass


def handle_order(cloud: Cloud, order: dict[str, Any]) -> None:
    """解析 → 规划 → 逐个执行 → 上传产物。"""
    from studio import intake
    from studio.server import _fill_defaults
    from tools.tool_registry import registry
    from studio import i18n

    oid = order["id"]
    title = order.get("title") or "未命名订单"
    print(f"\n[{datetime.now():%H:%M:%S}] 认领订单 {oid} · {title}")

    try:
        brief = intake.parse_brief(order.get("brief") or "")
        if not brief.scenes:
            cloud.send_plan(oid, {}, "failed", "没有解析出任何分镜，请检查内容格式")
            return
        plan = intake.build_plan(brief)
        note = f"解析出 {len(brief.scenes)} 个分镜"
        if plan["blocked_stages"]:
            note += f"；{'、'.join(plan['blocked_stages'])} 无可用工具（需配置密钥）"
        cloud.send_plan(oid, plan, "running", note)
        print(f"  分镜 {len(brief.scenes)} 个，工序 {len(plan['stages'])} 道")

        jobs = intake.plan_to_jobs(brief, plan)
        if not jobs:
            cloud.finish(oid, "failed", "没有可下发的任务：相关能力暂无可用工具")
            return

        registry.ensure_discovered()
        ok = failed = 0

        for idx, spec in enumerate(jobs, 1):
            job_id = f"{oid}-{idx:03d}"
            inputs = _fill_defaults(spec["tool"], dict(spec["inputs"]))
            base = {
                "id": job_id, "order_id": oid, "tool": spec["tool"],
                "tool_label": i18n.tool_name(spec["tool"]),
                "label": spec["label"], "inputs": inputs,
            }
            cloud.send_job({**base, "status": "running"})
            print(f"  [{idx}/{len(jobs)}] {spec['label']} · {spec['tool']}", end=" ", flush=True)

            started = time.time()
            try:
                tool = registry.get(spec["tool"])
                if tool is None:
                    raise ValueError(f"工具不存在: {spec['tool']}")
                result = tool.execute(inputs)
                elapsed = round(time.time() - started, 1)

                if result.success:
                    keys = []
                    for art in (result.artifacts or []):
                        p = Path(art)
                        if not p.exists():
                            continue
                        key = f"{oid}/{p.name}"
                        cloud.upload(key, p)
                        keys.append(key)
                    cloud.send_job({**base, "status": "success", "artifacts": keys,
                                    "cost_usd": float(result.cost_usd or 0), "elapsed": elapsed})
                    ok += 1
                    print(f"✅ {elapsed}s · 上传 {len(keys)} 个产物")
                else:
                    cloud.send_job({**base, "status": "failed",
                                    "error": result.error or "工具返回失败", "elapsed": elapsed})
                    failed += 1
                    print(f"❌ {result.error}"[:120])
            except Exception as exc:
                elapsed = round(time.time() - started, 1)
                cloud.send_job({**base, "status": "failed",
                                "error": f"{type(exc).__name__}: {exc}", "elapsed": elapsed})
                failed += 1
                print(f"❌ {type(exc).__name__}: {exc}"[:120])

        summary = f"完成 {ok} 个，失败 {failed} 个"
        if plan.get("deferred_stages"):
            summary += f"；{'、'.join(plan['deferred_stages'])} 需前序产物，未自动执行"
        cloud.finish(oid, "done" if ok and not failed else ("failed" if not ok else "done"), summary)
        print(f"  订单收尾：{summary}")

    except Exception as exc:
        traceback.print_exc(limit=3)
        cloud.finish(oid, "failed", f"代理异常：{type(exc).__name__}: {exc}"[:400])


def main() -> int:
    parser = argparse.ArgumentParser(prog="studio.agent", description="AI 视频工厂 · 本地渲染代理")
    parser.add_argument("--url", required=True, help="云端控制面地址，如 https://xxx.workers.dev")
    parser.add_argument("--token", default=os.environ.get("AVF_TOKEN", ""), help="访问令牌")
    parser.add_argument("--name", default=socket.gethostname(), help="代理名称")
    parser.add_argument("--once", action="store_true", help="只处理一个订单后退出")
    args = parser.parse_args()

    if not args.token:
        print("错误：缺少访问令牌（--token 或环境变量 AVF_TOKEN）")
        return 2

    _prepare_env()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    from lib.env_loader import load_env
    try:
        load_env()
    except Exception:
        pass

    cloud = Cloud(args.url, args.token, args.name)

    try:
        cloud._request("GET", "/api/health")
    except urllib.error.HTTPError as exc:
        print(f"连接失败（HTTP {exc.code}）：令牌可能不正确")
        return 1
    except Exception as exc:
        print(f"连接失败：{exc}")
        return 1

    print("=" * 60)
    print("  AI 视频工厂 · 本地渲染代理")
    print(f"  云端: {args.url}")
    print(f"  代理: {args.name}   根目录: {ROOT}")
    print("  等待订单中… (Ctrl+C 退出)")
    print("=" * 60)

    idle_since = time.time()
    while True:
        try:
            order = cloud.claim()
            if order:
                handle_order(cloud, order)
                idle_since = time.time()
                if args.once:
                    return 0
                time.sleep(POLL_BUSY)
            else:
                # 空闲时每分钟打一次心跳日志，便于确认代理还活着
                if time.time() - idle_since > 60:
                    print(f"[{datetime.now():%H:%M:%S}] 空闲等待中…")
                    idle_since = time.time()
                time.sleep(POLL_IDLE)
        except KeyboardInterrupt:
            print("\n代理已停止")
            return 0
        except Exception as exc:
            print(f"轮询异常（{type(exc).__name__}: {exc}），10 秒后重试")
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
