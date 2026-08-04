"""FastAPI app for the Studio console.

Binds to localhost only — it can execute tools and write .env, so it must not
be exposed on a network interface.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from studio import catalog as catalog_mod
from studio import env_manager
from studio import i18n
from studio import intake as intake_mod
from studio.jobs import QUEUE, ROOT, OUTPUT_ROOT, default_output_path

UI_DIR = Path(__file__).resolve().parent / "ui"

MEDIA_EXT = {".mp4", ".mov", ".webm", ".mkv", ".mp3", ".wav", ".m4a", ".aac",
             ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".srt", ".vtt", ".json", ".txt"}


class JobRequest(BaseModel):
    tool: str
    inputs: dict[str, Any] = {}
    label: str = ""


class BatchRequest(BaseModel):
    tool: str
    rows: list[dict[str, Any]] = []
    label: str = ""


class KeyRequest(BaseModel):
    updates: dict[str, str] = {}


class IntakeRequest(BaseModel):
    text: str = ""
    budget_usd: Optional[float] = None
    want_subtitle: bool = True


class IntakeRunRequest(BaseModel):
    text: str = ""
    budget_usd: Optional[float] = None
    want_subtitle: bool = True
    overrides: dict[str, str] = {}   # 阶段名 -> 手动指定的工具


def _safe_path(rel: str) -> Path:
    """Resolve a repo-relative path, refusing anything outside the repo."""
    p = (ROOT / rel).resolve()
    if not str(p).startswith(str(ROOT)):
        raise HTTPException(status_code=403, detail="路径越界")
    if not p.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return p


def _doctor() -> dict[str, Any]:
    """Environment check — what's installed, what's missing, what's free to add."""
    def cmd(name: str) -> dict[str, Any]:
        path = shutil.which(name)
        version = ""
        if path:
            try:
                out = subprocess.run([path, "--version"], capture_output=True,
                                     text=True, timeout=8)
                version = (out.stdout or out.stderr).splitlines()[0][:80] if (out.stdout or out.stderr) else ""
            except Exception:
                version = ""
        return {"name": name, "ok": bool(path), "path": path or "", "version": version}

    def mod(name: str) -> dict[str, Any]:
        try:
            __import__(name)
            return {"name": name, "ok": True}
        except Exception:
            return {"name": name, "ok": False}

    node = cmd("node")
    node_major = 0
    if node["ok"] and node["version"]:
        try:
            node_major = int(node["version"].lstrip("v").split(".")[0])
        except ValueError:
            node_major = 0

    return {
        "commands": [cmd("ffmpeg"), cmd("ffprobe"), node, cmd("npx"), cmd("piper"), cmd("manim")],
        "node_major": node_major,
        "node_ok_for_hyperframes": node_major >= 22,
        "modules": [mod(m) for m in
                    ("faster_whisper", "yt_dlp", "youtube_transcript_api", "pygments",
                     "rembg", "torch", "transformers", "PIL", "numpy")],
        # Free, local, no API key — safe to offer as one-click installs.
        "free_installs": [
            {"pkg": "faster-whisper", "unlocks": ["transcriber"], "desc": "本地语音转文字（字幕基础）", "size": "中"},
            {"pkg": "yt-dlp", "unlocks": ["video_downloader"], "desc": "下载 YouTube 等 1000+ 站点视频", "size": "小"},
            {"pkg": "youtube-transcript-api", "unlocks": ["transcript_fetcher"], "desc": "抓取 YouTube 字幕", "size": "小"},
            {"pkg": "Pygments Pillow", "unlocks": ["code_snippet"], "desc": "代码片段美化配图", "size": "小"},
            {"pkg": "rembg", "unlocks": ["bg_remove"], "desc": "AI 抠图去背景", "size": "大"},
            {"pkg": "manim", "unlocks": ["math_animate"], "desc": "数学/公式动画（3Blue1Brown 风格）", "size": "大"},
        ],
    }


def create_app() -> FastAPI:
    app = FastAPI(title="AI 视频工厂 · 工作台", version="1.0.0")

    # ---- meta ----

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "root": str(ROOT), "time": time.time()}

    @app.get("/api/catalog")
    def get_catalog(refresh: bool = False) -> dict[str, Any]:
        return {
            "tools": catalog_mod.catalog(refresh=refresh),
            "summary": catalog_mod.summary(refresh=False),
        }

    @app.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        return _doctor()

    @app.get("/api/i18n")
    def labels() -> dict[str, Any]:
        return i18n.payload()

    # ---- 智能派单 ----

    @app.post("/api/intake/upload")
    async def intake_upload(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="文件过大（上限 8MB）")
        try:
            text = intake_mod.extract_text(file.filename or "", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"filename": file.filename, "chars": len(text), "text": text}

    @app.post("/api/intake/plan")
    def intake_plan(req: IntakeRequest) -> dict[str, Any]:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="内容为空")
        brief = intake_mod.parse_brief(req.text)
        if not brief.scenes:
            raise HTTPException(status_code=400, detail="没有解析出任何分镜，请检查内容")
        return intake_mod.build_plan(brief, budget=req.budget_usd,
                                     want_subtitle=req.want_subtitle)

    @app.post("/api/intake/run")
    def intake_run(req: IntakeRunRequest) -> dict[str, Any]:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="内容为空")
        brief = intake_mod.parse_brief(req.text)
        plan = intake_mod.build_plan(brief, budget=req.budget_usd,
                                     want_subtitle=req.want_subtitle)
        for stage in plan["stages"]:
            override = req.overrides.get(stage["stage"])
            if override:
                stage["tool"] = override
                stage["tool_label"] = i18n.tool_name(override)
                stage["reason"] = "手动指定"

        jobs = intake_mod.plan_to_jobs(brief, plan)
        if not jobs:
            raise HTTPException(status_code=400, detail="没有可下发的任务（相关能力暂无可用工具）")

        submitted = []
        for spec in jobs:
            inputs = _fill_defaults(spec["tool"], spec["inputs"])
            job = QUEUE.submit(spec["tool"], inputs, label=spec["label"])
            submitted.append(job.id)
        return {"submitted": len(submitted), "job_ids": submitted, "plan": plan}

    # ---- keys ----

    @app.get("/api/keys")
    def get_keys() -> dict[str, Any]:
        s = catalog_mod.summary()
        unlock_map = {e["key"]: e["unlocks"] for e in s["key_unlocks"]}
        return {"keys": env_manager.status(unlock_map)}

    @app.post("/api/keys")
    def set_keys(req: KeyRequest) -> dict[str, Any]:
        result = env_manager.write_keys(req.updates)
        # Availability changes the moment a key lands.
        catalog_mod.catalog(refresh=True)
        return {**result, "summary": catalog_mod.summary()}

    # ---- jobs ----

    @app.post("/api/jobs")
    def create_job(req: JobRequest) -> dict[str, Any]:
        inputs = _fill_defaults(req.tool, req.inputs)
        job = QUEUE.submit(req.tool, inputs, label=req.label or req.tool)
        return job.public()

    @app.post("/api/batch")
    def create_batch(req: BatchRequest) -> dict[str, Any]:
        if not req.rows:
            raise HTTPException(status_code=400, detail="批量任务不能为空")
        rows = [_fill_defaults(req.tool, r) for r in req.rows]
        return QUEUE.submit_batch(req.tool, rows, label=req.label)

    @app.get("/api/jobs")
    def list_jobs(limit: int = 200, batch_id: str = "") -> dict[str, Any]:
        return {"jobs": QUEUE.list(limit=limit, batch_id=batch_id), "stats": QUEUE.stats()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = QUEUE.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return job.public()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        return {"cancelled": QUEUE.cancel(job_id)}

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = QUEUE.listen()

        async def gen():
            try:
                yield f"data: {json.dumps({'hello': True})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: q.get(timeout=15)
                        )
                        yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
                    except Exception:
                        yield ": keepalive\n\n"
            finally:
                QUEUE.unlisten(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- outputs ----

    @app.get("/api/outputs")
    def outputs(limit: int = 200) -> dict[str, Any]:
        files = []
        for base in (OUTPUT_ROOT, ROOT / "projects"):
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if p.is_file() and p.suffix.lower() in MEDIA_EXT and not p.name.startswith("_"):
                    try:
                        stat = p.stat()
                    except OSError:
                        continue
                    files.append({
                        "path": str(p.relative_to(ROOT)),
                        "name": p.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "ext": p.suffix.lower().lstrip("."),
                    })
            break  # OUTPUT_ROOT is inside projects/; one pass is enough
        files.sort(key=lambda f: -f["mtime"])
        return {"files": files[:limit], "total": len(files)}

    @app.get("/media/{rel:path}")
    def media(rel: str) -> FileResponse:
        p = _safe_path(rel)
        mime, _ = mimetypes.guess_type(str(p))
        return FileResponse(p, media_type=mime or "application/octet-stream")

    # ---- UI ----

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        f = UI_DIR / "index.html"
        if not f.exists():
            return HTMLResponse("<h1>UI 未安装</h1>", status_code=500)
        return HTMLResponse(f.read_text(encoding="utf-8"))

    @app.get("/ui/{name}")
    def ui_asset(name: str):
        p = (UI_DIR / name).resolve()
        if not str(p).startswith(str(UI_DIR)) or not p.exists():
            raise HTTPException(status_code=404, detail="not found")
        mime, _ = mimetypes.guess_type(str(p))
        return FileResponse(p, media_type=mime or "text/plain")

    return app


def _fill_defaults(tool_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Drop blanks and auto-assign output_path when the schema wants one."""
    schema = {}
    for item in catalog_mod.catalog():
        if item["name"] == tool_name:
            schema = item.get("input_schema") or {}
            break
    props = (schema.get("properties") or {})

    cleaned = {k: v for k, v in inputs.items() if v not in ("", None, [])}

    if "output_path" in props and not cleaned.get("output_path"):
        suffix = _guess_suffix(tool_name, props)
        cleaned["output_path"] = default_output_path(tool_name, suffix)

    _resolve_piper_model(tool_name, cleaned)
    return cleaned


_CJK = re.compile(r"[一-鿿]")


def _resolve_piper_model(tool_name: str, inputs: dict[str, Any]) -> None:
    """Expand a bare Piper voice name to the downloaded .onnx path.

    piper_tts passes `model` straight to the CLI without --data-dir, so a bare
    name like "en_US-lessac-medium" only resolves if the model happens to sit
    in the working directory. Voices live in ~/.piper/models.

    When the caller didn't pick a voice, choose by script language — Chinese
    narration read by an English voice is unusable.
    """
    if tool_name != "piper_tts":
        return
    models_dir = Path.home() / ".piper" / "models"
    model = str(inputs.get("model") or "").strip()

    if not model:
        text = str(inputs.get("text") or "")
        model = "zh_CN-huayan-medium" if _CJK.search(text) else "en_US-lessac-medium"

    if "/" in model or model.endswith(".onnx"):
        return
    candidate = models_dir / f"{model}.onnx"
    if candidate.exists():
        inputs["model"] = str(candidate)


def _guess_suffix(tool_name: str, props: dict[str, Any]) -> str:
    name = tool_name.lower()
    if any(k in name for k in ("tts", "music", "audio", "speech")):
        return ".wav" if "piper" in name else ".mp3"
    if "subtitle" in name:
        return ".srt"
    if any(k in name for k in ("image", "imagen", "flux", "diagram", "snippet", "card")):
        return ".png"
    return ".mp4"


app = create_app()
