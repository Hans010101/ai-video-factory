"""零密钥完整出片：脚本 → 旁白 → 动画画面 → 成片 MP4。

为什么需要这个：
    项目里 13 个图像生成工具、22 个视频生成工具**全部需要 API 密钥**，
    所以「先生成画面再合成」这条路在零密钥下是断的。但 Remotion 合成器
    自带 40 种数据/文字驱动的场景（标题、文字卡、数据卡、图表、KPI 网格
    等），配上 Piper 本地配音，完全不需要任何密钥就能产出成片。

    这个工具把那条路打通：解析分镜 → 逐镜配音 → 按音频时长排布画面 →
    拼接旁白 → 调 Remotion 渲染 → 输出 1920×1080 MP4。

注册进 registry 后，它在工具目录、任务队列、智能派单、云端代理里都是
一等公民，不需要任何特殊分支。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

ROOT = Path(__file__).resolve().parent.parent
COMPOSER_DIR = ROOT / "remotion-composer"

# 深色底 + 中国红点缀，和项目品牌一致；视频上深色底比白底更耐看。
THEMES: dict[str, dict[str, str]] = {
    "brand": {"bg": "#141418", "fg": "#F5F5F7", "accent": "#E8112D"},
    "slate": {"bg": "#0F172A", "fg": "#F8FAFC", "accent": "#F59E0B"},
    "light": {"bg": "#FFFFFF", "fg": "#1A1A1C", "accent": "#C8102E"},
}

TITLE_SECONDS = 2.6
TAIL_SECONDS = 1.2
MIN_CUT_SECONDS = 1.8


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float((out.stdout or "0").strip())
    except ValueError:
        return 0.0


def _concat_audio(parts: list[Path], out_path: Path) -> float:
    """把逐镜旁白拼成一条完整音轨。

    所有片段都来自同一个 Piper 模型，采样率与声道一致，用 concat 分离器
    直接流拷贝即可，不需要重编码。
    """
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # concat 分离器按「列表文件所在目录」解析相对路径，而列表文件在临时目录里，
    # 所以这里必须写绝对路径。
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        for p in parts:
            fh.write("file '{}'\n".format(p.resolve().as_posix().replace("'", r"'\''")))
        list_file = fh.name
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", str(out_path)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"拼接旁白失败：{(proc.stderr or '')[-400:]}")
    finally:
        Path(list_file).unlink(missing_ok=True)
    return _probe_duration(out_path)


MEDIA_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp")


def fetch_footage(query: str, out_dir: Path, index: int) -> Optional[dict[str, str]]:
    """从素材源找一段真实影像并下载，连同署名信息一起返回。

    优先用已配置密钥的策展库（Pexels 等），没有就退回免费公共档案
    （Wikimedia Commons、Archive.org、NASA、国会图书馆）。档案馆是关键词
    匹配，命中质量参差，这是源本身的特性，不是选型问题。

    返回 {"file", "source", "creator", "license", "url"}；找不到返回 None。
    """
    if not query.strip():
        return None
    try:
        from tools.video.stock_sources import available_sources
        from tools.video.stock_sources.base import SearchFilters
    except Exception:
        return None

    filters = SearchFilters(per_page=6)
    for source in available_sources():
        try:
            hits = source.search(query, filters)
        except Exception:
            continue
        for cand in (hits or [])[:3]:
            try:
                # 字段是 download_url，不是 url —— 取错会让图片素材被存成
                # .mp4，Remotion 按视频解析后直接失败。
                suffix = Path(str(getattr(cand, "download_url", "") or "").split("?")[0]).suffix.lower()
                if suffix not in MEDIA_SUFFIXES:
                    suffix = ".jpg" if getattr(cand, "kind", "") == "image" else ".mp4"
                target = out_dir / f"shot_{index:03d}{suffix}"
                got = source.download(cand, target)
                path = Path(got or target)
                if path.exists() and path.stat().st_size > 20_000:
                    return {
                        "file": path.name,
                        "source": getattr(cand, "source", "") or getattr(source, "name", ""),
                        "creator": getattr(cand, "creator", "") or "",
                        "license": getattr(cand, "license", "") or "",
                        "url": getattr(cand, "source_url", "") or "",
                    }
            except Exception:
                continue
    return None


def build_credits(shots: list[Optional[dict[str, str]]]) -> str:
    """生成片尾署名文案。

    素材站的使用条款普遍要求署名并回链，这里把用到的每一条素材的来源与
    作者列出来，避免「用了但没标」。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for shot in shots:
        if not shot:
            continue
        who = shot.get("creator") or "—"
        src = (shot.get("source") or "").replace("_", " ").title()
        key = f"{src}|{who}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{src} · {who}")
    return "素材来源：" + "；".join(lines) if lines else ""


def build_cuts(scenes: list[dict[str, Any]], durations: list[float],
               title: str, theme: dict[str, str],
               footage: Optional[list[Optional[dict[str, str]]]] = None,
               credits: str = "") -> list[dict[str, Any]]:
    """按每镜旁白的真实时长排布画面，画面与声音自然对齐。"""
    cuts: list[dict[str, Any]] = []
    t = 0.0

    if title:
        cuts.append({
            "id": "title", "type": "hero_title", "source": "",
            "in_seconds": 0.0, "out_seconds": TITLE_SECONDS,
            "text": title, "subtitle": "",
            "backgroundColor": theme["bg"], "color": theme["fg"],
        })
        t = TITLE_SECONDS

    for i, (sc, dur) in enumerate(zip(scenes, durations)):
        span = max(dur, MIN_CUT_SECONDS)
        narration = (sc.get("narration") or "").strip()
        visual = (sc.get("visual") or "").strip()
        # 有画面建议时用它做副标题：它描述的正是这一镜该呈现什么，
        # 直接展示比丢掉更有信息量。
        cut: dict[str, Any] = {
            "id": f"scene-{i + 1}",
            "source": "",
            "in_seconds": round(t, 3),
            "out_seconds": round(t + span, 3),
            "backgroundColor": theme["bg"],
            "color": theme["fg"],
            "accentColor": theme["accent"],
        }
        shot = (footage or [None] * len(scenes))[i] if footage else None
        if shot:
            # 有真实素材时用它当画面，旁白文字走 overlay 叠在上面。
            # 注意：组件的类型判断在 source 之前，设了文字类型就不会渲染素材。
            cut["source"] = f"studio/{shot['file']}"
            cut["source_in_seconds"] = 0
        else:
            # 没找到素材才退回文字画面。text_card 只渲染 text，
            # callout 能同时呈现画面提示与旁白。
            if narration and visual:
                cut.update({"type": "callout", "text": narration, "title": visual})
            else:
                cut.update({"type": "text_card", "text": narration or visual or f"第 {i + 1} 镜"})
        cuts.append(cut)
        t += span

    if cuts:
        # 用了外部素材就必须署名 —— 素材站条款普遍要求，也是申请 API 时的承诺。
        tail_span = 3.2 if credits else TAIL_SECONDS
        cuts.append({
            "id": "tail", "type": "text_card", "source": "",
            "in_seconds": round(t, 3), "out_seconds": round(t + tail_span, 3),
            "text": credits, "subtitle": "",
            "fontSize": 34 if credits else None,
            "backgroundColor": theme["bg"], "color": theme["fg"],
        })
    return cuts


def build_overlays(cuts: list[dict[str, Any]], scenes: list[dict[str, Any]],
                   theme: dict[str, str]) -> list[dict[str, Any]]:
    """给用了真实素材的镜头叠上旁白文字 —— 素材画面本身没有文字信息。"""
    overlays: list[dict[str, Any]] = []
    by_id = {c["id"]: c for c in cuts}
    for i, sc in enumerate(scenes):
        cut = by_id.get(f"scene-{i + 1}")
        if not cut or not cut.get("source"):
            continue  # 文字画面本身已经带文案，不需要再叠
        narration = (sc.get("narration") or "").strip()
        if not narration:
            continue
        overlays.append({
            "type": "section_title",
            "in_seconds": cut["in_seconds"] + 0.2,
            "out_seconds": max(cut["out_seconds"] - 0.2, cut["in_seconds"] + 0.6),
            "text": narration,
            "accentColor": theme["accent"],
            "position": "bottom",
        })
    return overlays


class ZeroKeyVideo(BaseTool):
    """脚本直出成片，全程本地、零 API 密钥。"""

    name = "zero_key_video"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "studio"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg", "cmd:ffprobe", "cmd:npx", "cmd:piper"]
    install_instructions = (
        "需要 FFmpeg、Node 22+（npx）与 Piper。\n"
        "Piper 语音模型：.venv/bin/python -m piper.download_voices "
        "zh_CN-huayan-medium --download-dir ~/.piper/models"
    )

    capabilities = ["script_to_video", "offline_generation", "narrated_explainer"]
    best_for = [
        "零 API 密钥出完整成片",
        "文字/数据驱动的解说与宣传片",
    ]
    not_good_for = [
        "需要实拍或 AI 生成画面的片子",
        "强视觉冲击的电影级镜头",
    ]
    supports = {"offline": True, "narration": True, "multilingual": True}
    resource_profile = ResourceProfile()

    input_schema = {
        "type": "object",
        "required": ["brief"],
        "properties": {
            "brief": {"type": "string", "description": "脚本原文，支持「场景N」「旁白：」「画面：」标记"},
            "title": {"type": "string", "description": "片头标题，留空则自动取脚本首行"},
            "theme": {"type": "string", "enum": ["brand", "slate", "light"], "default": "brand"},
            "use_footage": {"type": "boolean", "default": True,
                            "description": "按画面建议从免费素材源（Wikimedia/Archive.org/NASA 等）抓真实影像；关闭则纯文字画面"},
            "voice_model": {"type": "string", "description": "Piper 音色，留空按脚本语言自动选择"},
            "output_path": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"video": {"type": "string"}}}

    def get_status(self) -> ToolStatus:
        try:
            self.check_dependencies()
        except Exception:
            return ToolStatus.UNAVAILABLE
        if not (COMPOSER_DIR / "node_modules").exists():
            return ToolStatus.DEGRADED
        return ToolStatus.AVAILABLE

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        from studio import intake  # 延迟导入，避免与 registry 发现期循环依赖

        brief_text = (inputs.get("brief") or "").strip()
        if not brief_text:
            return ToolResult(success=False, error="brief 为空")

        brief = intake.parse_brief(brief_text)
        if not brief.scenes:
            return ToolResult(success=False, error="没有解析出任何分镜")

        theme = THEMES.get(inputs.get("theme") or "brand", THEMES["brand"])
        # 全程用绝对路径：Remotion 以 remotion-composer/ 为工作目录，
        # ffmpeg 的 concat 列表按列表文件所在目录解析，相对路径两处都会失效。
        raw_out = Path(inputs.get("output_path") or (ROOT / "projects" / "studio" / "zero_key_video.mp4"))
        if not raw_out.is_absolute():
            raw_out = ROOT / raw_out
        out_path = raw_out.with_suffix(".mp4").resolve()
        work = out_path.parent / f".{out_path.stem}_work"
        work.mkdir(parents=True, exist_ok=True)

        # ---- 1. 逐镜配音 ----
        from tools.tool_registry import registry
        registry.ensure_discovered()
        piper = registry.get("piper_tts")
        if piper is None:
            return ToolResult(success=False, error="piper_tts 不可用")

        model = (inputs.get("voice_model") or "").strip()
        if not model:
            has_cjk = any("一" <= ch <= "鿿" for ch in brief_text)
            model = "zh_CN-huayan-medium" if has_cjk else "en_US-lessac-medium"
        if "/" not in model and not model.endswith(".onnx"):
            candidate = Path.home() / ".piper" / "models" / f"{model}.onnx"
            if candidate.exists():
                model = str(candidate)

        wavs: list[Path] = []
        durations: list[float] = []
        for i, sc in enumerate(brief.scenes, 1):
            text = (sc.narration or sc.visual or "").strip()
            if not text:
                continue
            wav = work / f"scene_{i:03d}.wav"
            r = piper.execute({"text": text, "model": model, "output_path": str(wav)})
            if not r.success or not wav.exists():
                return ToolResult(success=False, error=f"第{i}镜配音失败：{r.error}")
            wavs.append(wav)
            durations.append(_probe_duration(wav))

        if not wavs:
            return ToolResult(success=False, error="没有可配音的文本")

        # ---- 2. 拼接旁白 ----
        # 放进 remotion-composer/public/ 后用相对路径引用（staticFile）。
        # 走 file:// 绝对路径的话，仓库路径里的中文字符不会被百分号编码，
        # Remotion 的资源下载器会直接失败。
        public_dir = COMPOSER_DIR / "public" / "studio"
        public_dir.mkdir(parents=True, exist_ok=True)
        narration = public_dir / f"{out_path.stem}_narration.wav"
        total_audio = _concat_audio(wavs, narration)
        narration_src = f"studio/{narration.name}"

        # ---- 3. 抓真实素材（免费源，无需密钥）----
        scenes = [s.as_dict() for s in brief.scenes][: len(durations)]
        footage: list[Optional[dict[str, str]]] = []
        if inputs.get("use_footage", True):
            for i, sc in enumerate(scenes):
                query = (sc.get("visual") or sc.get("narration") or "").strip()
                footage.append(fetch_footage(query, public_dir, i + 1))
        else:
            footage = [None] * len(scenes)
        credits = build_credits(footage)

        # ---- 4. 构造 Remotion props ----
        cuts = build_cuts(scenes, durations, inputs.get("title") or brief.title,
                          theme, footage, credits)
        overlays = build_overlays(cuts, scenes, theme)
        # 旁白从片头之后开始，和画面对齐
        props = {
            "theme": "flat-motion-graphics",
            "cuts": cuts,
            "overlays": overlays,
            "captions": [],
            "audio": {
                "narration": {
                    "src": narration_src,
                    "volume": 1.0,
                    "offsetSeconds": TITLE_SECONDS if (inputs.get("title") or brief.title) else 0,
                }
            },
        }
        props_path = work / "props.json"
        props_path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---- 4. 渲染 ----
        npx = shutil.which("npx")
        if not npx:
            return ToolResult(success=False, error="找不到 npx（需要 Node 22+）")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [npx, "remotion", "render", "src/index.tsx", "Explainer",
             str(out_path), "--props", str(props_path), "--codec", "h264"],
            cwd=COMPOSER_DIR, capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode != 0 or not out_path.exists():
            tail = (proc.stderr or proc.stdout or "")[-600:]
            return ToolResult(success=False, error=f"Remotion 渲染失败：{tail}")

        return ToolResult(
            success=True,
            data={
                "video": str(out_path),
                "scenes": len(scenes),
                "narration_seconds": round(total_audio, 2),
                "video_seconds": round(cuts[-1]["out_seconds"], 2) if cuts else 0,
                "voice_model": Path(model).stem,
                "footage_used": sum(1 for f in footage if f),
                "footage_sources": sorted({f["source"] for f in footage if f}),
                "credits": credits,
            },
            artifacts=[str(out_path)],
            duration_seconds=round(time.time() - started, 1),
        )


def register() -> None:
    """把本地工具挂进 registry —— 它不在 tools/ 包里，不会被自动发现。"""
    from tools.tool_registry import registry
    registry.ensure_discovered()
    if registry.get(ZeroKeyVideo.name) is None:
        registry.register(ZeroKeyVideo())
