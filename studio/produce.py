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
import re
import shutil
import subprocess
import tempfile
import time
import uuid
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

# 静态图片不加运镜就是一张死图撑几秒。轮换使用避免每镜都同一种推法 ——
# 连续同向运动看久了比不动还难受。
KEN_BURNS_CYCLE = ("ken-burns", "zoom-in", "pan-right", "zoom-out", "pan-left", "parallax")
# 镜头间的交叉淡入淡出时长（秒）。太长会糊，太短等于硬切。
TRANSITION_SECONDS = 0.5


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


# 参考真人口播实测：单句中位 10 字、语速 5.95 字/秒。
# 把整段 50 多字一次喂给 TTS，模型会读成一条长句，没有停顿也没有重音；
# 拆成短句分别合成再用静音拼接，才能还原「一顿一顿」的口播节奏。
_CLAUSE_SPLIT = re.compile(r"(?<=[，。！？；：、])")
_BURST_TARGET = 12      # 单个语音块的目标字数
_BURST_MAX = 18


def narration_bursts(text: str) -> list[str]:
    """把一段旁白切成适合逐句合成的短块。"""
    out: list[str] = []
    buf = ""
    for piece in _CLAUSE_SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        if not buf:
            buf = piece
        elif len(buf) + len(piece) <= _BURST_TARGET:
            buf += piece
        else:
            out.append(buf)
            buf = piece
    if buf:
        out.append(buf)

    # 没有标点的超长块只能按字数硬断
    final: list[str] = []
    for b in out:
        while len(b) > _BURST_MAX:
            final.append(b[:_BURST_TARGET])
            b = b[_BURST_TARGET:]
        if b:
            final.append(b)
    return final


def _silence(seconds: float, out_path: Path, sample_rate: int = 44100) -> bool:
    """生成一段静音。

    必须显式指定编码器：anullsrc 默认输出的采样格式 mp3 容器不接受，
    不指定会报 "Could not write header (incorrect codec parameters?)"。
    另外静音片段要和语音片段编码一致，否则 concat 流拷贝会失败。
    """
    codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if out_path.suffix == ".mp3" \
        else ["-c:a", "pcm_s16le"]
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r={sample_rate}:cl=mono",
         "-t", f"{seconds:.3f}", *codec, str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode == 0 and out_path.exists()


def _normalize_narration(path: Path) -> bool:
    """把旁白响度统一到播客标准（-16 LUFS）。

    逐镜生成的音频响度会有起伏，直接拼接后忽大忽小。做一遍 loudnorm 还能
    压掉句首的爆音、抬起气声段落，听感稳很多。
    失败时保留原文件 —— 响度不完美也好过没有旁白。
    """
    tmp = path.with_name(path.stem + "_norm" + path.suffix)
    # 链路顺序有讲究：先去嘶声再做响度，反过来的话 loudnorm 已经把齿音
    # 抬起来了，再压就是压过的声音。
    #   deesser        —— 抑制 4-10kHz 的齿音（TTS 的「吱吱声」主要在这）
    #   highpass 80Hz  —— 切掉人声用不到的低频隆隆声，听感更干净
    #   loudnorm       —— I=-16 播客口播标准，TP=-1.5 留削峰余量
    # 顺序：先切低频隆隆 → 压齿音 → 再做响度。
    # loudnorm 会把整体抬约 8dB，齿音也跟着上来，所以必须放在它之前压。
    chain = ("highpass=f=80,"
             "deesser=i=0.6:m=0.5:f=0.3,"
             "loudnorm=I=-16:TP=-1.5:LRA=11")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-af", chain, "-ar", "44100", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        # 老版本 ffmpeg 没有 deesser，退回不含它的链路
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ar", "44100", str(tmp)],
            capture_output=True, text=True, timeout=300,
        )
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
        tmp.replace(path)
        return True
    tmp.unlink(missing_ok=True)
    return False


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
        # 不能用 concat 分离器：它不做重采样，要求各段参数完全一致。
        # DashScope 返回 24kHz PCM（哪怕文件名是 .mp3），我们插入的静音是
        # 44.1kHz —— 混着拼会产生可听见的杂音。
        # 改用 concat 滤镜：每一路先各自重采样对齐，再拼，格式差异被吃掉。
        cmd = ["ffmpeg", "-y"]
        for p in parts:
            cmd += ["-i", str(p.resolve())]
        n = len(parts)
        filt = "".join(f"[{i}:a]aformat=sample_fmts=s16:sample_rates=44100:"
                       f"channel_layouts=mono[a{i}];" for i in range(n))
        filt += "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
        cmd += ["-filter_complex", filt, "-map", "[out]",
                "-ar", "44100", "-ac", "1", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"拼接旁白失败：{(proc.stderr or '')[-400:]}")
    finally:
        Path(list_file).unlink(missing_ok=True)
    return _probe_duration(out_path)


MEDIA_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp")

# 中文约 5.5 字/秒，英文约 2.5 词/秒。一个镜头超过十几秒不换画面就很难看，
# 所以按这个速率把长旁白切成多个镜头。
_CN_CHARS_PER_SEC = 5.5
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])\s*")


def split_long_scenes(scenes: list[dict[str, Any]],
                      max_seconds: float = 11.0) -> list[dict[str, Any]]:
    """把过长的旁白按句子边界切成多个镜头。

    整篇文章常常只写一段旁白配一条画面建议，直接出片就是几十秒不换画面。
    切分只在句末标点处发生，不会把句子拦腰截断；画面建议由切出的各镜共享，
    后面检索时会取不同候选，保证画面有变化。
    """
    max_chars = int(max_seconds * _CN_CHARS_PER_SEC)
    out: list[dict[str, Any]] = []

    for sc in scenes:
        narration = (sc.get("narration") or "").strip()
        if len(narration) <= max_chars:
            out.append(sc)
            continue

        pieces = [p.strip() for p in _SENTENCE_END.split(narration) if p.strip()]
        chunks: list[str] = []
        buf = ""
        for piece in pieces:
            # 单句本身就超长时独立成镜，不硬切
            if not buf:
                buf = piece
            elif len(buf) + len(piece) <= max_chars:
                buf = f"{buf}{piece}"
            else:
                chunks.append(buf)
                buf = piece
        if buf:
            chunks.append(buf)

        for i, chunk in enumerate(chunks):
            out.append({
                **sc,
                # 保留原节拍名，切出来的子镜共享同一个节拍身份
                "title": sc.get("title") or "",
                "narration": chunk,
                # 画面建议只保留在首镜？不 —— 每镜都要有画面，共享同一条建议，
                # 检索时按 variant 取不同候选即可。
                "visual": sc.get("visual") or "",
                "_variant": i,
            })

    for i, sc in enumerate(out, 1):
        sc["index"] = i
    return out


def fetch_footage(query: str, out_dir: Path, index: int,
                  prefix: str = "shot", variant: int = 0) -> Optional[dict[str, str]]:
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

    # 素材库的标签几乎全是英文，中文查询命中率与精准度都明显更低。
    # 只转检索词，脚本、旁白、字幕都不受影响。
    from studio.translate import to_english
    search_query = to_english(query)

    # 必须限定横构图与最小宽度：成片是 1920x1080，竖屏素材按 cover 裁剪后
    # 主体会被切掉 —— 人物只剩脖子和衣领，这正是「画面不全」的来源。
    # 时长下限过滤掉一两秒的碎片，那种素材撑不满一个镜头。
    filters = SearchFilters(per_page=12, orientation="landscape",
                            min_width=1280, min_duration=4)
    for source in available_sources():
        try:
            hits = source.search(search_query, filters)
        except Exception:
            continue
        # 同一条画面建议切出的多个镜头共享检索结果，按 variant 错开取用，
        # 否则整段都是同一个画面，等于没切。
        pool = list(hits or [])
        # 二道兜底：部分源站不认 orientation 过滤，这里按实际宽高再筛一次。
        # 宽高比低于 1.2 的（竖屏、方形）裁到 16:9 一定会切掉主体。
        wide = [c for c in pool
                if (getattr(c, "width", 0) or 0) >= (getattr(c, "height", 1) or 1) * 1.2]
        pool = wide or pool  # 全被筛掉时退回原结果，总比没画面强

        if variant and pool:
            pool = pool[variant % len(pool):] + pool[:variant % len(pool)]
        for cand in pool[:3]:
            try:
                # 字段是 download_url，不是 url —— 取错会让图片素材被存成
                # .mp4，Remotion 按视频解析后直接失败。
                suffix = Path(str(getattr(cand, "download_url", "") or "").split("?")[0]).suffix.lower()
                if suffix not in MEDIA_SUFFIXES:
                    suffix = ".jpg" if getattr(cand, "kind", "") == "image" else ".mp4"
                target = out_dir / f"{prefix}_{index:03d}{suffix}"
                got = source.download(cand, target)
                path = Path(got or target)
                if path.exists() and path.stat().st_size > 20_000:
                    return {
                        "file": path.name,
                        "source": getattr(cand, "source", "") or getattr(source, "name", ""),
                        "creator": getattr(cand, "creator", "") or "",
                        "license": getattr(cand, "license", "") or "",
                        "url": getattr(cand, "source_url", "") or "",
                        "query": search_query,
                    }
            except Exception:
                continue
    return None


# 兜底生成器按「便宜优先」排序。图像比视频便宜一到两个数量级：
# Imagen $0.04/张，而 Veo $2.0/段 —— 5 镜的片子就是 $0.2 对 $10。
# gemini_image 排在 google_imagen 前面：后者调的 Imagen `:predict` 端点已对
# 新 Google 账户停止开放（404 no longer available to new users）。
# 出图按「成本 × 质量」排序，实测同一提示词的结果：
#   dashscope_image  $0.020  阿里云 Qwen-Image，情绪表达最贴中文语境
#   flux_image(dev)  $0.030  质量相当
#   gemini_image     免费    额度小，用完就 429
#   flux-pro/v1.1    $0.050  质量没有明显优势，不值这个价
#   openai_image     $0.211  最贵，且该账户有账单硬上限
# 把便宜的排前面能把每条片子的画面成本从 $0.40 压到 $0.16。
AI_FALLBACK_TOOLS = {
    "image": ("dashscope_image", "gemini_image", "flux_image",
              "google_imagen", "openai_image", "image_gen"),
    "video": ("gemini_omni_video", "sora_video", "veo_video"),
}

# 生成失败的原因收集器。配额用尽、密钥失效这类问题必须让人看见，
# 静默退回文字画面会让人以为「功能没做」。
_GEN_ERRORS: list[str] = []

# 等一会儿就能过的错。403/401/402 不在其列 —— 那是余额或权限问题，
# 重试多少次都一样，只会白白拖长出片时间。
_TRANSIENT = ("429", "too many requests", "rate limit", "ratelimit",
              "throttl", "timeout", "timed out", "502", "503", "504")


def _is_transient(message: str) -> bool:
    low = (message or "").lower()
    return any(k in low for k in _TRANSIENT)


# 出图缓存。提示词、模型、尺寸、seed 全同 = 结果必然相同，没有理由再买一次。
# 调脚本、调剪辑、调配音时会把同一条片子重出很多遍，画面部分不该跟着重复付费。
# 只在给了 seed 时启用 —— 没给 seed 的调用本来就是要每次都不一样。
_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "shots"


def _cache_path(tool_name: str, inputs: dict[str, Any], suffix: str) -> Optional[Path]:
    if inputs.get("seed") is None:
        return None
    import hashlib
    payload = {k: v for k, v in inputs.items() if k != "output_path"}
    payload["__tool"] = tool_name
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:32]
    return _CACHE_DIR / f"{digest}{suffix}"


def generate_shot(query: str, out_dir: Path, index: int, kind: str,
                  budget_left: Optional[float],
                  prefix: str = "shot", negative: str = "",
                  seed: Optional[int] = None, prefer: str = ""
                  ) -> tuple[Optional[dict[str, str]], float]:
    """素材源没命中时，用 AI 生成一张图或一段视频顶上。

    返回 (shot, 实际花费)。预算不够或没有可用生成器时返回 (None, 0.0)，
    由调用方退回文字画面 —— 宁可画面朴素，也不能悄悄超预算。
    """
    if not query.strip():
        return None, 0.0
    from tools.tool_registry import registry
    registry.ensure_discovered()

    suffix = ".png" if kind == "image" else ".mp4"
    chain = AI_FALLBACK_TOOLS.get(kind, ())
    if prefer:
        # 前面几镜用哪个模型出的，后面就接着用哪个。各家画风差别很大，
        # 中途换厂商会让同一条片子出现两种画风，比省下的几分钱刺眼得多。
        chain = (prefer,) + tuple(n for n in chain if n != prefer)
    for name in chain:
        tool = registry.get(name)
        if tool is None or tool.get_status().value not in ("available", "degraded"):
            continue

        inputs: dict[str, Any] = {"prompt": query,
                                  "output_path": str(out_dir / f"{prefix}_{index:03d}{suffix}")}
        if seed is not None:
            # 只为可复现：同一脚本每次出片结果一致，方便定位问题。
            # 一致性不靠它 —— 靠 shot_prompt 里逐镜复述的身份锚点。
            # 反过来，全片共用一个 seed 会把同一个坏构图复制到每一镜
            # （出现过九镜里父女的衣服全部对调），所以调用方按镜号错开。
            inputs["seed"] = seed
        if name == "dashscope_image":
            # 尺寸用星号分隔且要精确 16:9；watermark=False 关掉右下角水印。
            # prompt_extend 会让模型自行扩写提示词，风格容易跑偏，关掉。
            inputs.update({
                # 横评过 qwen-image-2.0-pro / wan2.7-image / z-image-turbo：
                # wan2.7 出图最好（服装绑定准、16:9 满构图、环境细节完整），
                # 也是这个账户唯一还有额度的一档，其余全部 Arrearage。
                "model": "wan2.7-image",
                "size": "1440*810",
                "watermark": False,
                "prompt_extend": False,
                "negative_prompt": negative or (
                    "text, letters, chinese characters, words, watermark, "
                    "logo, signature, cut off head, deformed hands, extra limbs"),
            })
        if name == "gemini_image":
            inputs.setdefault("aspect_ratio", "16:9")
        if name == "flux_image":
            # 正向提示词里写 "no text" 压不住 —— FLUX 仍会在画框、招牌上生成
            # 乱码汉字，非常出戏。负向提示词才是有效手段。
            inputs["negative_prompt"] = negative or (
                "text, letters, chinese characters, words, captions, subtitles, "
                "signage, poster text, watermark, logo, signature, "
                "cropped subject, cut off head, extra limbs, deformed hands, blurry"
            )
            # 必须是严格 16:9，否则进片按 cover 裁剪会切掉主体。
            # FLUX pro v1.1 的单边上限是 1440：要 1920x1080 会被压成
            # 1440x1056（比例 1.36），要 1536x864 会被压成 1440x864（1.67），
            # 两种都不是 16:9，进片仍会裁掉主体。1440x810 才是限额内的精确
            # 16:9。Remotion 放大到 1080p 对插画完全够用。
            inputs.update({"width": 1440, "height": 810})
        cached = _cache_path(name, inputs, suffix)
        if cached and cached.exists() and cached.stat().st_size > 10_000:
            shutil.copy2(cached, inputs["output_path"])
            return {"file": Path(inputs["output_path"]).name, "tool": name,
                    "source": f"AI 生成 · {name}（缓存）",
                    "creator": "", "license": "", "url": ""}, 0.0

        try:
            estimate = float(tool.estimate_cost(inputs) or 0.0)
        except Exception:
            estimate = 0.0
        if budget_left is not None and estimate > budget_left:
            continue  # 换更便宜的，都超预算就放弃

        # 429 是暂时的，退避重试就能过；配额耗尽、密钥失效才该换厂商。
        # 不区分的话，一次限流就把整片后半段推给了另一家模型，画风分家。
        result = None
        for attempt in range(3):
            try:
                result = tool.execute(inputs)
            except Exception as exc:
                _GEN_ERRORS.append(f"{name}: {type(exc).__name__}: {exc}"[:160])
                result = None
                break
            if result.success or not _is_transient(str(result.error)):
                break
            if attempt < 2:
                time.sleep(6 * (attempt + 1))
        if result is None:
            continue
        if not result.success:
            # 静默跳过会让人以为「没生成」，实际是配额用尽/密钥失效这类
            # 需要处理的问题。记下来在结果里回报。
            _GEN_ERRORS.append(f"{name}: {str(result.error)[:140]}")
            continue
        path = Path((result.artifacts or [inputs["output_path"]])[0])
        if not path.exists() or path.stat().st_size < 10_000:
            _GEN_ERRORS.append(f"{name}: 产出文件缺失或过小")
            continue

        if cached:
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, cached)
            except OSError:
                pass  # 缓存写不进去不该拖垮出片

        spent = float(result.cost_usd or estimate)
        return {
            "file": path.name,
            "tool": name,
            "source": f"AI 生成 · {name}",
            "creator": "",
            "license": "",
            "url": "",
        }, spent
    return None, 0.0


# 按中文自然度排序，Piper 永远垫底兜底。
# 「可用」只代表密钥在，不代表能调通 —— 额度耗尽会在真正请求时才报 402/429，
# 所以必须在运行时逐个试，不能只看状态。
# 中文原生厂商排在最前：ElevenLabs 的音色都是英语母语者，多语种模型说
# 中文始终带口音和别扭的重音，这是训练数据决定的，调参救不了。
# 阿里云 Qwen-TTS 与豆包都是中文原生训练，播音质感完全不同。
NARRATOR_CHAIN = ("dashscope_tts", "doubao_tts", "elevenlabs_tts",
                  "google_tts", "openai_tts", "piper_tts")

# 中文原生音色的默认选择与播报风格
_CN_TTS_DEFAULTS = {
    "dashscope_tts": {
        "model": "qwen3-tts-instruct-flash",   # instruct 版支持自然语言指导播报风格
        "voice": "Ethan",                      # 男声
        "language_type": "Chinese",
        # 参照真人口播的实测特征写指令：短句推进、重音落在关键词、
        # 句尾略微下沉。「像在跟朋友讲道理」比「像纪录片解说」更贴这类
        # 观点内容 —— 纪录片腔太端着，缺少代入感。
        "instructions": "自然、有分寸的中年男声，像在跟朋友认真讲道理。"
                        "短句推进，关键词加重语气，句尾略微下沉。"
                        "有情绪起伏但不煽情，不用播音腔，不拖长音。",
    },
    "doubao_tts": {},
}

# ElevenLabs 工具默认用 Rachel（21m00Tcm4TlvDq8ikWAM），那是「音色库音色」，
# 免费账户通过 API 调用会直接 402：
#     Free users cannot use library voices via the API.
# 账户自有的 premade 音色则完全可用，所以这里主动选一个自有音色。
_EL_LIBRARY_DEFAULT = "21m00Tcm4TlvDq8ikWAM"

# 默认男声：Eric，沉稳可信，适合观点与情感解说。
# 账户自有音色，免费额度可用。改音色只需传 voice_model。
_EL_PREFERRED_VOICE = "cjVigY5qzO86Huf0OWal"

# 中文观点类内容的配音调参：
#   stability 偏高 —— 低了会忽快忽慢、情绪飘忽，听感不稳
#   similarity_boost 偏高 —— 保持音色一致，逐镜生成时不会前后不像同一个人
#   style 压低 —— 风格化会带来夸张的抑扬顿挫，对理性叙述是干扰
#   speed 略慢 —— 中文信息密度高，原速容易赶，留出理解余量
_EL_VOICE_SETTINGS = {
    "stability": 0.62,
    "similarity_boost": 0.85,
    "style": 0.08,
    "speed": 0.94,
}


def elevenlabs_voice() -> str:
    """挑一个账户自有的 ElevenLabs 音色，避开音色库音色。"""
    import json as _json
    import os as _os
    import urllib.request as _rq

    key = _os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        return ""
    if _EL_PREFERRED_VOICE:
        return _EL_PREFERRED_VOICE
    try:
        req = _rq.Request("https://api.elevenlabs.io/v1/voices")
        req.add_header("xi-api-key", key)
        req.add_header("User-Agent", "AIVideoFactory/1.0")
        voices = _json.loads(_rq.urlopen(req, timeout=30).read()).get("voices") or []
    except Exception:
        return ""

    owned = [v for v in voices
             if v.get("category") in ("premade", "cloned", "generated", "professional")
             and v.get("voice_id") != _EL_LIBRARY_DEFAULT]
    return owned[0].get("voice_id", "") if owned else ""


def narrator_candidates(preferred: str = "auto") -> list[tuple[Any, str]]:
    """返回可尝试的配音工具链，按优先级排序。"""
    from tools.tool_registry import registry
    registry.ensure_discovered()

    order = list(NARRATOR_CHAIN)
    if preferred not in ("", "auto"):
        order = [preferred] + [n for n in order if n != preferred]

    out: list[tuple[Any, str]] = []
    for name in order:
        tool = registry.get(name)
        if tool is not None and tool.get_status().value in ("available", "degraded"):
            out.append((tool, name))
    return out


def _tts_with_retry(tool: Any, job: dict[str, Any], attempts: int = 3) -> Any:
    """带退避的 TTS 调用。

    短句分合成会把请求数放大三四倍（8 镜的片子要发近 30 次），很容易撞上
    供应商的速率限制。瞬时限流应该等一下重试，而不是直接判定这家不可用
    ——那会让整片回落到音质更差的备选，得不偿失。
    """
    last = None
    for i in range(attempts):
        last = tool.execute(job)
        if last.success:
            return last
        msg = str(last.error or "")
        transient = any(k in msg for k in ("429", "Too Many", "rate", "timeout", "503", "502"))
        if not transient or i == attempts - 1:
            return last
        time.sleep(1.5 * (i + 1))
    return last


def _tts_inputs(name: str, text: str, out_path: Path, voice: str) -> dict[str, Any]:
    job: dict[str, Any] = {"text": text, "output_path": str(out_path)}

    if name in _CN_TTS_DEFAULTS:
        job.update(_CN_TTS_DEFAULTS[name])
        if voice:
            job["voice"] = voice   # 手动指定时覆盖默认音色
        return job

    if name == "piper_tts":
        job["model"] = voice
    elif voice:
        job["voice_id"] = voice
    if name == "elevenlabs_tts":
        # 多语种模型，中文才有可用的发音
        job.setdefault("model_id", "eleven_multilingual_v2")
        for k, v in _EL_VOICE_SETTINGS.items():
            job.setdefault(k, v)
    return job


def fetch_music(mood: str, seconds: float, out_dir: Path, prefix: str) -> Optional[dict[str, Any]]:
    """找一段背景音乐。

    没有配乐的解说片会显得很空 —— 这是「效果差」里最容易被忽略的一项。
    优先用免费的 Pixabay 音乐库，没有再用生成。
    """
    from tools.tool_registry import registry
    registry.ensure_discovered()

    target = out_dir / f"{prefix}_bgm.mp3"

    pix = registry.get("pixabay_music")
    if pix is not None and pix.get_status().value in ("available", "degraded"):
        try:
            # min_duration 卡太死会搜不到：曲库里超过一分钟的免费曲目本就少，
            # 上限压到 45 秒，短了循环播放即可（props 里已开 loop）。
            r = pix.execute({"query": mood,
                             "min_duration": min(max(int(seconds * 0.6), 10), 45),
                             "output_path": str(target)})
            if r.success and target.exists() and target.stat().st_size > 20_000:
                return {"file": target.name, "source": "Pixabay Music", "cost": 0.0}
            _GEN_ERRORS.append(f"pixabay_music: {str(r.error)[:120]}")
        except Exception as exc:
            _GEN_ERRORS.append(f"pixabay_music: {type(exc).__name__}: {exc}"[:140])

    gen = registry.get("music_gen")
    if gen is not None and gen.get_status().value in ("available", "degraded"):
        try:
            r = gen.execute({"prompt": mood, "duration_seconds": min(int(seconds) + 3, 120),
                             "force_instrumental": True, "output_path": str(target)})
            if r.success and target.exists() and target.stat().st_size > 20_000:
                return {"file": target.name, "source": "AI 生成配乐",
                        "cost": float(r.cost_usd or 0.0)}
            _GEN_ERRORS.append(f"music_gen: {str(r.error)[:120]}")
        except Exception as exc:
            _GEN_ERRORS.append(f"music_gen: {type(exc).__name__}: {exc}"[:140])
    return None


# 画面风格预设。实拍素材的问题是构图不可控 —— 检索到什么就是什么，
# 人物可能只拍到半张脸。生成式风格能保证每镜构图完整、风格统一。
VISUAL_STYLES: dict[str, dict[str, str]] = {
    "footage": {
        "label": "实拍素材",
        "prompt": "",
        "note": "从 Pexels 等素材库检索真实影像，免费但构图不可控",
    },
    "comic": {
        "label": "漫画插画",
        # 反复强调无文字：模型很容易在招牌、海报上生成乱码汉字，非常出戏。
        # 限定人物数量，否则它会自作主张塞满背景人物，冲淡主体。
        "prompt": ("Modern Chinese editorial comic illustration, clean bold linework, "
                   "flat muted color palette, warm neutral tones with one accent color, "
                   "expressive but restrained faces, at most two people, subjects fully "
                   "visible and centered, generous negative space, calm domestic setting. "
                   "Absolutely no text, no letters, no signage, no captions, no watermark, "
                   "no logos anywhere in the image. Scene: "),
        "note": "AI 生成统一风格插画，构图完整可控，约 $0.05/张",
    },
    "cinematic": {
        "label": "电影质感",
        "prompt": ("Cinematic photographic still, 16:9 widescreen composition, shallow depth "
                   "of field, natural window light, muted film color grade, subject fully "
                   "framed with headroom, no text, no watermark. Scene: "),
        "note": "AI 生成写实画面，构图完整，约 $0.04/张",
    },
    "ink": {
        "label": "水墨留白",
        "prompt": ("Minimal Chinese ink-wash illustration, generous negative space, "
                   "restrained brushwork, muted ink tones with a single warm accent, "
                   "16:9 composition, contemplative mood, no text, no watermark. Scene: "),
        "note": "AI 生成水墨风，适合情感与哲思类内容，约 $0.04/张",
    },
}


def generate_styled_shot(scene_text: str, style: str, out_dir: Path, index: int,
                         prefix: str, budget_left: Optional[float],
                         narration: str = "", cast: str = "",
                         seed: Optional[int] = None, prefer: str = "",
                         medium: str = ""
                         ) -> tuple[Optional[dict[str, str]], float]:
    """按选定风格生成整片统一的画面。

    与 generate_shot 的区别：那个是「检索没命中时的兜底」，这个是「主动
    选择生成式风格」—— 整片每一镜都走同一套风格提示词，画面语言统一，
    且构图由提示词约束（16:9、主体完整、留白），不会出现实拍素材那种
    人物被裁掉的情况。
    """
    preset = VISUAL_STYLES.get(style)
    if not preset or not preset["prompt"]:
        return None, 0.0

    from studio.translate import to_english
    from studio import shot_check, shot_prompt

    # 按拍摄简报公式组装：主体动作 → 镜头 → 光 → 介质。
    # 情绪从中文旁白里读，决定机位与光源 —— 此前每镜共用一套提示词，
    # 讲隐忍疲惫时会配出一家人其乐融融的画面。
    subject = to_english(scene_text)[:200]
    built = shot_prompt.build(narration or scene_text, style, subject,
                              cast=cast, medium_override=medium)

    # 双人镜头有约四分之一概率把两人的衣服互换（FLUX 多主体属性绑定不牢），
    # 且由 seed 决定，改提示词写法救不了。只能出完自检，翻车就换 seed 重打。
    spent_total = 0.0
    for attempt in range(3 if built.get("headcount") == 2 else 1):
        shot, spent = generate_shot(
            built["prompt"], out_dir, index, "image", budget_left, prefix,
            negative=built["negative_prompt"],
            seed=None if seed is None else seed + attempt * 1000,
            prefer=prefer)
        spent_total += spent
        if budget_left is not None:
            budget_left = max(budget_left - spent, 0.0)
        if shot is None:
            return None, spent_total
        if not shot_check.wardrobe_swapped(out_dir / shot["file"]):
            return shot, spent_total  # 没换装，或判不出来 —— 都收下
    return shot, spent_total  # 三次都翻车就认了，别把预算烧在这一镜上


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
        raw_source = shot.get("source") or ""
        who = (shot.get("creator") or "").strip()

        if raw_source.startswith("AI 生成"):
            # AI 生成没有摄影师，标出模型即可；也不能套 .title()，
            # 那会把「AI」变成「Ai」。
            entry = raw_source
        else:
            src = raw_source.replace("_", " ").title()
            entry = f"{src} · {who}" if who else src

        if entry in seen:
            continue
        seen.add(entry)
        lines.append(entry)
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
            # 有真实素材时用它当画面，旁白文字走字幕层。
            # 注意：组件的类型判断在 source 之前，设了文字类型就不会渲染素材。
            cut["source"] = f"studio/{shot['file']}"
            cut["source_in_seconds"] = 0
            # 静态图片给 Ken Burns 运镜；视频本身有运动，只需要转场。
            is_still = Path(shot["file"]).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            if is_still:
                cut["animation"] = KEN_BURNS_CYCLE[i % len(KEN_BURNS_CYCLE)]
            else:
                cut["transition_in"] = "fade"
                cut["transition_out"] = "fade"
                cut["transition_duration"] = TRANSITION_SECONDS
        else:
            # 没找到素材才退回文字画面。只能放旁白 —— visual 是画面指导，
            # 里面带着「统一风格：现代中国都市寓言插画、深墨绿…」这类提示词
            # 原文，是给制作看的，打在成片上非常出戏。
            cut.update({"type": "text_card", "text": narration or f"第 {i + 1} 镜"})
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


# 一条字幕的长度上限。中文一行看得舒服的极限约 14 字，超过就该断。
_CAPTION_MAX_CHARS = 14
_CAPTION_SPLIT = re.compile(r"(?<=[，。！？、；：,.!?;:])")


def _caption_units(text: str) -> list[str]:
    """把一段旁白切成适合上屏的短句。"""
    units: list[str] = []
    buf = ""
    for piece in _CAPTION_SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        if not buf:
            buf = piece
        elif len(buf) + len(piece) <= _CAPTION_MAX_CHARS:
            buf += piece
        else:
            units.append(buf)
            buf = piece
    if buf:
        units.append(buf)

    # 没有标点的长句只能按字数硬断
    out: list[str] = []
    for u in units:
        while len(u) > _CAPTION_MAX_CHARS * 2:
            out.append(u[:_CAPTION_MAX_CHARS])
            u = u[_CAPTION_MAX_CHARS:]
        out.append(u)
    return out


def captions_from_bursts(burst_timeline: list[tuple[str, float, float]],
                         offset_seconds: float) -> list[dict[str, Any]]:
    """用每个短句的真实时长生成字幕。

    短句分合成之后，每一块的实际时长是量出来的而不是估的 —— 字幕直接
    按这个时间轴走，和声音严丝合缝，不会像按字数等比分配那样越到后面
    偏得越多。短句本身也正好是一条字幕的长度。
    """
    return [
        {"word": text, "startMs": int((offset_seconds + start) * 1000),
         "endMs": int((offset_seconds + end) * 1000)}
        for text, start, end in burst_timeline
        if text.strip()
    ]


def build_captions(scenes: list[dict[str, Any]], durations: list[float],
                   offset_seconds: float) -> list[dict[str, Any]]:
    """按每镜音频的真实时长生成同步字幕。

    此前是把整段旁白当 overlay 一次性铺在画面上 —— 363 字糊满三分之一屏幕，
    既读不了也难看。字幕应该跟着声音走，一次只出一两句。

    没有逐字对齐数据，就按字数在该镜时长内等比分配。误差在半秒内，观感上
    完全够用，而且不需要额外跑一次语音识别。
    """
    captions: list[dict[str, Any]] = []
    t = offset_seconds

    for sc, dur in zip(scenes, durations):
        text = (sc.get("narration") or "").strip()
        if not text or dur <= 0:
            t += dur
            continue
        units = _caption_units(text)
        total_chars = sum(len(u) for u in units) or 1
        cursor = t
        for unit in units:
            span = dur * (len(unit) / total_chars)
            captions.append({
                "word": unit,
                "startMs": int(cursor * 1000),
                "endMs": int((cursor + span) * 1000),
            })
            cursor += span
        t += dur
    return captions


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
            "voice_provider": {"type": "string", "enum": ["auto", "elevenlabs_tts", "piper_tts"],
                               "default": "auto",
                               "description": "auto 优先 ElevenLabs（中文自然度高），无密钥时退回本地 Piper"},
            "quality_check": {"type": "boolean", "default": True,
                              "description": "交付前自动核验：轨道、分辨率、拼接杂音、字幕同步、静默降级"},
            "quality_check_deep": {"type": "boolean", "default": True,
                                   "description": "深度核验会转录成片比对字幕时间轴，慢但能抓出错位"},
            "caption_font_size": {"type": "number", "default": 56,
                                  "description": "字幕字号（1080p 下 56 较清晰，手机观看也够大）"},
            "captions": {"type": "boolean", "default": True,
                         "description": "生成跟随旁白的同步字幕（而不是把整段文案铺在画面上）"},
            "music": {"type": "boolean", "default": True, "description": "自动配背景音乐"},
            "burst_pacing": {"type": "boolean", "default": True,
                             "description": "按短句分别合成再拼接，还原真人口播的顿挫感（参考真人实测：单句中位 10 字）"},
            "burst_gap": {"type": "number", "default": 0.22,
                          "description": "句间停顿秒数，调大更沉稳、调小更紧凑"},
            "normalize_audio": {"type": "boolean", "default": True,
                                "description": "旁白响度统一到 -16 LUFS，避免逐镜生成后忽大忽小"},
            "music_mood": {"type": "string",
                           "description": "配乐风格描述，留空用「calm cinematic ambient emotional piano」"},
            "auto_split": {"type": "boolean", "default": True,
                           "description": "旁白过长时按句子边界自动切成多个镜头，避免几十秒不换画面"},
            "max_shot_seconds": {"type": "number", "default": 11,
                                 "description": "单镜最长秒数，超过就切分"},
            "visual_style": {"type": "string", "enum": ["comic", "cinematic", "ink", "footage"],
                             "default": "comic",
                             "description": "footage=检索实拍（免费但构图不可控）；comic/cinematic/ink=AI 生成统一风格，构图完整，约 $0.04/镜"},
            "use_footage": {"type": "boolean", "default": True,
                            "description": "按画面建议检索实拍素材（配了 PEXELS_API_KEY 走策展库，否则走公共档案馆）；关闭则纯文字画面"},
            "ai_fallback": {"type": "string", "enum": ["off", "image", "video"], "default": "off",
                            "description": "素材检索没命中时用 AI 生成兜底。会产生费用：图像约 $0.04/张，视频 $0.5–2/段。off 时退回文字画面"},
            "budget_usd": {"type": "number",
                           "description": "AI 生成的总花费上限（美元）。超出后不再生成，改用文字画面"},
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

        # 整篇文章常常只写成一段旁白配一条画面建议。不切分的话，几十秒的
        # 旁白全程只有一个画面。切分必须在配音之前，配音要按切好的镜逐条生成。
        raw_scenes = [s.as_dict() for s in brief.scenes]
        max_shot = float(inputs.get("max_shot_seconds") or 11.0)
        scene_dicts = (split_long_scenes(raw_scenes, max_shot)
                       if inputs.get("auto_split", True) else raw_scenes)

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
        chain = narrator_candidates(inputs.get("voice_provider") or "auto")
        if not chain:
            return ToolResult(success=False, error="没有可用的配音工具")

        has_cjk = any("一" <= ch <= "鿿" for ch in brief_text)
        piper_voice = (inputs.get("voice_model") or "").strip()
        if not piper_voice:
            piper_voice = "zh_CN-huayan-medium" if has_cjk else "en_US-lessac-medium"
        if "/" not in piper_voice and not piper_voice.endswith(".onnx"):
            cand = Path.home() / ".piper" / "models" / f"{piper_voice}.onnx"
            if cand.exists():
                piper_voice = str(cand)

        # 先用第一句探路：额度耗尽这类问题只有真正请求才会暴露（402/429），
        # 一句话就能确定哪个提供商真的能用，避免跑到一半才失败。
        narrator = narrator_name = None
        probe_text = next((s.get("narration") or s.get("visual") or ""
                           for s in scene_dicts if (s.get("narration") or s.get("visual"))), "")
        tts_notes: list[str] = []
        el_voice = ""
        for tool, name in chain:
            if name == "piper_tts":
                voice = piper_voice
            elif name == "elevenlabs_tts":
                el_voice = el_voice or (inputs.get("voice_model") or "") or elevenlabs_voice()
                voice = el_voice
            else:
                voice = inputs.get("voice_model") or ""
            probe_out = work / f"probe_{name}{'.wav' if name == 'piper_tts' else '.mp3'}"
            try:
                r = _tts_with_retry(tool, _tts_inputs(name, probe_text[:40], probe_out, voice))
            except Exception as exc:
                tts_notes.append(f"{name}: {type(exc).__name__}")
                continue
            if r.success and probe_out.exists():
                narrator, narrator_name = tool, name
                break
            tts_notes.append(f"{name}: {str(r.error)[:70]}")

        if narrator is None:
            return ToolResult(success=False,
                              error="所有配音工具都调用失败 —— " + "；".join(tts_notes))

        wavs: list[Path] = []
        durations: list[float] = []
        voiced: list[dict[str, Any]] = []
        # (短句文本, 相对音轨起点的开始秒, 结束秒) —— 字幕据此精确对齐
        burst_timeline: list[tuple[str, float, float]] = []
        scene_clock = 0.0
        ext = ".wav" if narrator_name == "piper_tts" else ".mp3"
        if narrator_name == "piper_tts":
            voice = piper_voice
        elif narrator_name == "elevenlabs_tts":
            voice = el_voice
        elif narrator_name in _CN_TTS_DEFAULTS:
            voice = inputs.get("voice_model") or ""   # 空则用 _CN_TTS_DEFAULTS 里的默认音色
        else:
            voice = inputs.get("voice_model") or ""
        for i, sc in enumerate(scene_dicts, 1):
            text = (sc.get("narration") or sc.get("visual") or "").strip()
            if not text:
                continue
            wav = work / f"scene_{i:03d}{ext}"

            if inputs.get("burst_pacing", True):
                # 按短句分别合成再拼接，还原真人口播的顿挫感
                bursts = narration_bursts(text)
                pieces: list[Path] = []
                gap_len = float(inputs.get("burst_gap") or 0.22)
                cursor = scene_clock   # 该镜在整条音轨上的起点
                for bi, burst in enumerate(bursts, 1):
                    bp = work / f"scene_{i:03d}_b{bi:02d}{ext}"
                    br = _tts_with_retry(narrator, _tts_inputs(narrator_name, burst, bp, voice))
                    if not br.success or not bp.exists():
                        return ToolResult(success=False,
                                          error=f"第{i}镜第{bi}句配音失败（{narrator_name}）：{br.error}")
                    pieces.append(bp)
                    # 记录这句的真实起止，字幕据此对齐（不再按字数估算）
                    bd = _probe_duration(bp)
                    burst_timeline.append((burst, cursor, cursor + bd))
                    cursor += bd
                    # 句间插一小段静音；末句不插，避免镜尾拖沓
                    if bi < len(bursts):
                        gap = work / f"scene_{i:03d}_g{bi:02d}{ext}"
                        if _silence(gap_len, gap):
                            pieces.append(gap)
                            cursor += gap_len
                _concat_audio(pieces, wav)
            else:
                r = _tts_with_retry(narrator, _tts_inputs(narrator_name, text, wav, voice))
                if not r.success or not wav.exists():
                    return ToolResult(success=False,
                                      error=f"第{i}镜配音失败（{narrator_name}）：{r.error}")

            wavs.append(wav)
            scene_dur = _probe_duration(wav)
            durations.append(scene_dur)
            voiced.append(sc)
            scene_clock += scene_dur

        if not wavs:
            return ToolResult(success=False, error="没有可配音的文本")

        # ---- 2. 拼接旁白 ----
        # 放进 remotion-composer/public/ 后用相对路径引用（staticFile）。
        # 走 file:// 绝对路径的话，仓库路径里的中文字符不会被百分号编码，
        # Remotion 的资源下载器会直接失败。
        public_dir = COMPOSER_DIR / "public" / "studio"
        public_dir.mkdir(parents=True, exist_ok=True)
        # 每次出片用独立前缀：public/ 是所有并发任务共享的目录，固定用
        # shot_001 这种名字会让同时跑的任务互相覆盖，而且上一轮遗留的
        # 同名不同扩展名文件（.png / .mp4）也会污染这一轮。
        run_id = f"{out_path.stem}_{uuid.uuid4().hex[:8]}"
        # 后缀必须跟随 TTS 产物：云端 TTS 出 mp3，写死 .wav 会让 concat
        # 的流拷贝把 MP3 数据塞进 WAV 容器，直接报
        # "Could not write header (incorrect codec parameters?)"。
        narration = public_dir / f"{run_id}_narration{ext}"
        # 片头标题占用的时间必须用真实静音垫出来。
        # 不能靠 props 里的 audio.narration.offsetSeconds —— Remotion 的
        # Audio 组件只读 src 和 volume，那个字段会被静默忽略，结果就是
        # 音频从 0 秒就播、字幕却晚了一个片头时长，全片对不上。
        title_offset = TITLE_SECONDS if (inputs.get("title") or brief.title) else 0
        parts = list(wavs)
        if title_offset > 0:
            lead = work / f"lead_silence{ext}"
            if _silence(title_offset, lead):
                parts.insert(0, lead)

        total_audio = _concat_audio(parts, narration)
        normalized = _normalize_narration(narration) if inputs.get("normalize_audio", True) else False
        if normalized:
            total_audio = _probe_duration(narration)
        narration_src = f"studio/{narration.name}"

        # ---- 3. 抓真实素材（免费源，无需密钥）----
        # voiced 与 durations / wavs 严格一一对应（跳过了没有文本的镜）
        scenes = voiced
        _GEN_ERRORS.clear()
        footage: list[Optional[dict[str, str]]] = []
        ai_mode = str(inputs.get("ai_fallback") or "off").lower()
        budget = inputs.get("budget_usd")
        budget_left = float(budget) if budget not in (None, "") else None
        spent_total = 0.0
        ai_count = 0

        # 默认走漫画插画：实拍素材库对情感/观点类内容匹配不上，检索到的
        # 多是通用城市空镜；生成式插画能精确表达「客厅里沉默的两个人」。
        style = str(inputs.get("visual_style") or "comic").lower()

        # 全片共用一组人物身份和一个 seed —— 模型不记得上一镜是谁，
        # 一致性只能靠把同一段外貌描述钉进每一镜来维持。
        from studio import shot_prompt as _sp
        cast = str(inputs.get("cast") or "") or _sp.detect_cast(brief_text)
        shot_seed = inputs.get("seed")
        shot_seed = int(shot_seed) if shot_seed else _sp.cast_seed(brief_text)
        locked_tool = ""  # 第一镜出图成功后锁定厂商，全片画风统一
        # 脚本自带的「统一风格」。只翻译一次，全片每一镜共用同一段英文，
        # 逐镜翻译会得到措辞不同的译文，画风照样会飘。
        script_medium = ""
        if brief.style:
            from studio.translate import to_english
            script_medium = to_english(brief.style)[:220]

        styled_queries: list[tuple[str, str]] = []
        if inputs.get("use_footage", True):
            for i, sc in enumerate(scenes):
                # 同一条画面建议切出的多镜，如果都用同一个检索词，画面只能靠
                # variant 错开，相关性还是差。把该镜自己的旁白拼进检索词，
                # 让每一镜的画面贴合它当下讲的内容。
                base = (sc.get("visual") or "").strip()
                own = (sc.get("narration") or "").strip()
                query = f"{base} {own[:40]}".strip() if base and own else (base or own)

                if style != "footage":
                    # 生成式风格：整片统一，构图由提示词约束，不检索素材库。
                    # 只喂画面建议，不喂旁白原句 —— query 里拼旁白是为了让
                    # 素材检索更贴题，但对出图是有害的：中文出图模型会把整句
                    # 话排版成画面里的标题和带引线的说明文字。旁白仍然单独
                    # 传给 narration 用来判定情绪。
                    subject_text = base or own
                    styled_queries.append((subject_text, own))
                    shot, spent = generate_styled_shot(
                        subject_text, style, public_dir, i + 1, run_id, budget_left,
                        narration=own, cast=cast, seed=shot_seed + i,
                        prefer=locked_tool, medium=script_medium)
                    if shot:
                        locked_tool = shot.get("tool") or locked_tool
                        ai_count += 1
                        spent_total += spent
                        if budget_left is not None:
                            budget_left = max(budget_left - spent, 0.0)
                    footage.append(shot)
                    continue

                shot = fetch_footage(query, public_dir, i + 1, run_id,
                                     variant=int(sc.get("_variant") or 0))
                # 素材库没命中才动用 AI 生成 —— 检索免费，生成要花钱。
                if shot is None and ai_mode in ("image", "video"):
                    shot, spent = generate_shot(query, public_dir, i + 1, ai_mode,
                                                budget_left, run_id)
                    if shot:
                        ai_count += 1
                        spent_total += spent
                        if budget_left is not None:
                            budget_left = max(budget_left - spent, 0.0)
                footage.append(shot)
        else:
            footage = [None] * len(scenes)

        # ---- 3a. 缺图的镜头沿用邻近镜头的画面 ----
        # 出图失败时退回文字卡，正文和底部字幕是同一句话，看着像出了故障。
        # 剪辑师遇到缺镜是复用邻近画面换个运镜，不是往屏幕上打字。
        # 运镜按镜号取（KEN_BURNS_CYCLE[i % n]），复用后自然是另一个运镜。
        if styled_queries and len(styled_queries) == len(footage):
            for i, f in enumerate(footage):
                if f:
                    continue
                near = next((footage[j] for j in range(i - 1, -1, -1) if footage[j]),
                            None) or \
                    next((footage[j] for j in range(i + 1, len(footage)) if footage[j]),
                         None)
                if near:
                    footage[i] = dict(near)

        # ---- 3b. 补拍：把少数派画风的镜头统一掉 ----
        # 限流会让前几镜落在 A 家、后几镜落在 B 家，同一条片子出现两种画风。
        # 出图时无法预知谁会挂，所以只能事后按多数派重出少数派。
        if styled_queries and len(styled_queries) == len(footage):
            used = [f["tool"] for f in footage if f and f.get("tool")]
            if len(set(used)) > 1:
                from collections import Counter
                winner = Counter(used).most_common(1)[0][0]
                for i, f in enumerate(footage):
                    if not f or f.get("tool") in ("", None, winner):
                        continue
                    q, own = styled_queries[i]
                    # 换个文件名，重出失败时不至于把原来那张好图覆盖成半截文件。
                    # 仍以 run_id 开头，收尾清理的 glob 照样能删掉。
                    redo, spent = generate_styled_shot(
                        q, style, public_dir, i + 1, f"{run_id}r", budget_left,
                        narration=own, cast=cast, seed=shot_seed + i,
                        prefer=winner, medium=script_medium)
                    if redo and redo.get("tool") == winner:
                        footage[i] = redo
                        spent_total += spent
                        if budget_left is not None:
                            budget_left = max(budget_left - spent, 0.0)

        credits = build_credits(footage)

        # ---- 4. 构造 Remotion props ----
        cuts = build_cuts(scenes, durations, inputs.get("title") or brief.title,
                          theme, footage, credits)
        if not inputs.get("captions", True):
            captions = []
        elif burst_timeline:
            # 有真实短句时间轴就用它，比按字数估算准得多
            captions = captions_from_bursts(burst_timeline, title_offset)
        else:
            captions = build_captions(scenes, durations, title_offset)

        # ---- 4.5 配乐 ----
        total_seconds = cuts[-1]["out_seconds"] if cuts else 0
        music = None
        if inputs.get("music", True):
            mood = (inputs.get("music_mood") or "").strip() or \
                "calm cinematic ambient emotional piano"
            music = fetch_music(mood, total_seconds, public_dir, run_id)
            if music:
                spent_total += float(music.get("cost") or 0.0)
        # 旁白从片头之后开始，和画面对齐
        props = {
            "theme": "flat-motion-graphics",
            "cuts": cuts,
            "overlays": [],
            "captions": captions,
            # caption 单位是短句不是单词，一次出 2 条正好一行
            "captionsPerPage": int(inputs.get("captions_per_page") or 2),
            "captionFontSize": int(inputs.get("caption_font_size") or 56),
            "audio": {
                "narration": {
                    "src": narration_src,
                    "volume": 1.0,
                    "offsetSeconds": title_offset,
                },
                **({"music": {
                    "src": f"studio/{music['file']}",
                    # 压到旁白之下，只做垫底，不能盖住人声
                    "volume": 0.14,
                    "loop": True,
                    "fadeInSeconds": 1.5,
                    "fadeOutSeconds": 3.0,
                }} if music else {}),
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

        # ---- 交付前自检 ----
        # 注意顺序：必须在清理中间文件之前跑，质检要读旁白音轨。
        # 渲染返回成功不等于片子能看：字幕错位、拼接杂音、配乐悄悄没了，
        # 这些都只有核一遍才发现。把过去踩过的坑固化成检查项。
        qc_report: dict[str, Any] = {}
        if inputs.get("quality_check", True):
            try:
                from studio import qc
                qc_report = qc.inspect(
                    out_path,
                    data={"music": (music or {}).get("source") or "无",
                          "scenes": len(scenes),
                          "footage_used": sum(1 for f in footage if f),
                          "captions": len(captions),
                          "generation_errors": list(dict.fromkeys(_GEN_ERRORS))[:4],
                          "narrator_fallbacks": tts_notes},
                    captions=captions,
                    deep=bool(inputs.get("quality_check_deep", True)),
                    # 必须在清理中间文件之前跑：旁白音轨是干净的，
                    # 用它核对同步比转录混了配乐的成片可靠得多
                    narration=narration,
                    expected_lead=title_offset,
                )
            except Exception as exc:
                qc_report = {"passed": None, "issues": [
                    {"level": "warn", "item": "质检", "detail": f"质检未能运行：{exc}"}]}

        # 质检读完了才能清理 —— public/ 会被打进每次渲染的 webpack bundle，
        # 堆积几十 MB 旧素材会让后续渲染越来越慢。
        for leftover in public_dir.glob(f"{run_id}*"):
            try:
                leftover.unlink()
            except OSError:
                pass

        return ToolResult(
            success=True,
            data={
                "video": str(out_path),
                "qc": qc_report,
                "scenes": len(scenes),
                "narration_seconds": round(total_audio, 2),
                "video_seconds": round(cuts[-1]["out_seconds"], 2) if cuts else 0,
                "voice_model": Path(voice).stem if voice else "默认音色",
                "footage_used": sum(1 for f in footage if f),
                "footage_sources": sorted({f["source"] for f in footage if f}),
                "ai_generated": ai_count,
                "narrator": narrator_name,
                "narrator_fallbacks": tts_notes,
                "visual_style": style,
                "cast": cast,
                "seed": shot_seed,
                # 各镜实际落到哪个模型。混用会导致同片两种画风，
                # 不打出来只能靠肉眼猜是哪一镜跑偏了。
                "shot_tools": [(f or {}).get("tool") or "-" for f in footage],
                "generation_errors": list(dict.fromkeys(_GEN_ERRORS))[:4],
                "captions": len(captions),
                "music": (music or {}).get("source") or "无",
                "search_queries": [f.get("query") for f in footage if f and f.get("query")],
                "credits": credits,
            },
            artifacts=[str(out_path)],
            cost_usd=round(spent_total, 4),
            duration_seconds=round(time.time() - started, 1),
        )


def register() -> None:
    """把本地工具挂进 registry —— 它们不在 tools/ 包里，不会被自动发现。"""
    from tools.tool_registry import registry
    from studio.gemini_image import GeminiImage

    registry.ensure_discovered()
    for cls in (ZeroKeyVideo, GeminiImage):
        if registry.get(cls.name) is None:
            registry.register(cls())
