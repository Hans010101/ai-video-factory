"""Job queue: run tools on a worker pool, single or batched.

Tools are synchronous and blocking (they shell out to FFmpeg, Remotion, or
hit HTTP APIs), so a thread pool is the right shape. Every state change is
broadcast to listeners so the console can stream progress over SSE.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from tools.tool_registry import registry

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = ROOT / "projects" / "studio"
RUN_LOG = ROOT / "projects" / "studio" / "_runs.jsonl"

QUEUED, RUNNING, SUCCESS, FAILED, CANCELLED = "queued", "running", "success", "failed", "cancelled"


@dataclass
class Job:
    id: str
    tool: str
    inputs: dict[str, Any]
    status: str = QUEUED
    label: str = ""
    batch_id: str = ""
    batch_index: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: str = ""
    artifacts: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    log: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed"] = round(
            (self.finished_at or time.time()) - (self.started_at or self.created_at), 1
        )
        # Artifacts are absolute on disk; expose repo-relative for the UI.
        d["artifacts_rel"] = [_relative(a) for a in self.artifacts]
        return d


def _relative(p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return p


def default_output_path(tool_name: str, suffix: str = "") -> str:
    """Give a job a sane output location when the caller leaves it blank."""
    day = datetime.now().strftime("%Y%m%d")
    stamp = datetime.now().strftime("%H%M%S")
    out_dir = OUTPUT_ROOT / day / tool_name
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{tool_name}_{stamp}_{uuid.uuid4().hex[:6]}{suffix}")


class JobQueue:
    def __init__(self, workers: int = 3) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="studio")
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._listeners: list[queue.Queue] = []
        self.workers = workers

    # ---- listeners (SSE) ----

    def listen(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._listeners.append(q)
        return q

    def unlisten(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _broadcast(self, job: Job) -> None:
        payload = job.public()
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    # ---- submission ----

    def submit(self, tool: str, inputs: dict[str, Any], label: str = "",
               batch_id: str = "", batch_index: int = 0) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12], tool=tool, inputs=inputs,
            label=label, batch_id=batch_id, batch_index=batch_index,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._broadcast(job)
        self._pool.submit(self._run, job)
        return job

    def submit_batch(self, tool: str, rows: list[dict[str, Any]], label: str = "") -> dict[str, Any]:
        batch_id = uuid.uuid4().hex[:8]
        jobs = [
            self.submit(tool, row, label=f"{label or tool} #{i + 1}",
                        batch_id=batch_id, batch_index=i)
            for i, row in enumerate(rows)
        ]
        return {"batch_id": batch_id, "count": len(jobs), "job_ids": [j.id for j in jobs]}

    # ---- execution ----

    def _run(self, job: Job) -> None:
        if job.status == CANCELLED:
            return
        job.status = RUNNING
        job.started_at = time.time()
        job.log.append(f"[{datetime.now():%H:%M:%S}] 开始执行 {job.tool}")
        self._broadcast(job)

        try:
            registry.ensure_discovered()
            from studio.produce import register as _register_local
            _register_local()
            tool = registry.get(job.tool)
            if tool is None:
                raise ValueError(f"工具不存在: {job.tool}")

            result = tool.execute(job.inputs)

            job.data = result.data or {}
            job.artifacts = list(result.artifacts or [])
            job.cost_usd = float(result.cost_usd or 0.0)
            job.duration_seconds = float(result.duration_seconds or 0.0)
            if result.success:
                job.status = SUCCESS
                job.log.append(
                    f"[{datetime.now():%H:%M:%S}] 完成 · 产出 {len(job.artifacts)} 个文件"
                    + (f" · 成本 ${job.cost_usd:.4f}" if job.cost_usd else "")
                )
            else:
                job.status = FAILED
                job.error = result.error or "工具返回失败但未提供原因"
                job.log.append(f"[{datetime.now():%H:%M:%S}] 失败: {job.error}")
        except Exception as exc:
            job.status = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            job.log.append(f"[{datetime.now():%H:%M:%S}] 异常: {job.error}")
            job.log.append(traceback.format_exc(limit=4))
        finally:
            job.finished_at = time.time()
            self._broadcast(job)
            self._persist(job)

    def _persist(self, job: Job) -> None:
        try:
            RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            with RUN_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "id": job.id, "tool": job.tool, "status": job.status,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "cost_usd": job.cost_usd, "artifacts": job.artifacts,
                    "error": job.error, "batch_id": job.batch_id,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---- queries ----

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, limit: int = 200, batch_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))
        out = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None or (batch_id and job.batch_id != batch_id):
                continue
            out.append(job.public())
            if len(out) >= limit:
                break
        return out

    def cancel(self, job_id: str) -> bool:
        """Best effort: only queued jobs can be stopped, threads aren't killable."""
        job = self._jobs.get(job_id)
        if job and job.status == QUEUED:
            job.status = CANCELLED
            job.finished_at = time.time()
            job.log.append("已取消（执行前）")
            self._broadcast(job)
            return True
        return False

    def stats(self) -> dict[str, Any]:
        with self._lock:
            jobs = list(self._jobs.values())
        return {
            "total": len(jobs),
            "queued": sum(1 for j in jobs if j.status == QUEUED),
            "running": sum(1 for j in jobs if j.status == RUNNING),
            "success": sum(1 for j in jobs if j.status == SUCCESS),
            "failed": sum(1 for j in jobs if j.status == FAILED),
            "cost_usd": round(sum(j.cost_usd for j in jobs), 4),
            "workers": self.workers,
        }


QUEUE = JobQueue()
