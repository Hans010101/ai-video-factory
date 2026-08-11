"""成片自动质检。

这个模块存在的原因很直接：过去几轮暴露的问题全是「渲染成功但成片不能看」
——字幕晚 2.6 秒、拼接处有杂音、画面被裁掉主体、配乐悄悄没了。渲染返回
success 并不代表片子可用，必须在交付前自己核一遍。

每一条检查都对应一个真实踩过的坑，不是凭空设想的规则。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

OK, WARN, FAIL = "ok", "warn", "fail"


def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _issue(level: str, item: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"level": level, "item": item, "detail": detail, "fix": fix}


def check_streams(info: dict[str, Any]) -> list[dict[str, str]]:
    """轨道齐全、分辨率与帧率正确。"""
    out: list[dict[str, str]] = []
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video:
        out.append(_issue(FAIL, "视频轨", "成片没有视频轨"))
    else:
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        if (w, h) != (1920, 1080):
            out.append(_issue(WARN, "分辨率", f"{w}x{h}，预期 1920x1080"))
    if not audio:
        out.append(_issue(FAIL, "音频轨", "成片没有音频轨 —— 配音没进片"))
    return out


def check_audio_artifacts(path: Path) -> list[dict[str, str]]:
    """拼接杂音检测。

    踩过的坑：concat 分离器不做重采样，24kHz 的 TTS 和 44.1kHz 的静音混拼
    会在每个拼接点产生杂音。表现为逐样本的剧烈跳变。
    """
    import numpy as np
    import wave

    tmp = Path("/tmp/_qc_audio.wav")
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0 or not tmp.exists():
        return [_issue(WARN, "音频提取", "无法提取音轨做质检")]

    with wave.open(str(tmp)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if len(a) < 1000:
        return [_issue(FAIL, "音频内容", "音轨几乎是空的")]

    jumps = int((np.abs(np.diff(a)) > 0.3).sum())
    if jumps > 20:
        return [_issue(FAIL, "拼接杂音", f"检出 {jumps} 处剧烈跳变",
                       "音频拼接要用 concat 滤镜并 aformat 对齐采样率，不能用 concat 分离器")]
    if jumps > 0:
        return [_issue(WARN, "拼接杂音", f"检出 {jumps} 处跳变（阈值内）")]
    return []


def check_narration_lead(narration: Path, expected_lead: float,
                         tolerance: float = 0.25) -> list[dict[str, str]]:
    """核对旁白音轨的前导静音是否等于片头时长。

    比转录成片可靠得多：成片音轨里混了从 0 秒淡入的配乐，转录和能量检测
    都会把配乐当成语音起点，量不准。旁白音轨是干净的，直接量。

    这一项对应的坑：props 里的 audio.narration.offsetSeconds 会被 Remotion
    的 Audio 组件忽略，片头时长必须用真实静音垫进音轨。
    """
    import numpy as np
    import wave

    if expected_lead <= 0 or not narration.exists():
        return []
    tmp = Path("/tmp/_qc_nar.wav")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(narration),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    if not tmp.exists():
        return [_issue(WARN, "旁白前导", "无法解码旁白音轨")]

    with wave.open(str(tmp)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    win = int(sr * 0.02)
    if len(a) < win:
        return [_issue(FAIL, "旁白前导", "旁白音轨过短")]
    frames = a[: len(a) // win * win].reshape(-1, win)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    voiced = np.where(rms > 0.01)[0]
    actual = float(voiced[0] * 0.02) if len(voiced) else 0.0

    drift = abs(actual - expected_lead)
    if drift > tolerance:
        return [_issue(FAIL, "字幕同步",
                       f"旁白前导静音 {actual:.2f}s，应为片头时长 {expected_lead:.2f}s，"
                       f"偏差 {drift:.2f}s —— 字幕会整体错位",
                       "片头时长要用真实静音垫进旁白音轨（offsetSeconds 不生效）")]
    return []


def check_caption_sync(path: Path, captions: list[dict[str, Any]],
                       tolerance: float = 1.0) -> list[dict[str, str]]:
    """字幕与语音是否对齐。

    踩过的坑：props 里的 audio.narration.offsetSeconds 被 Remotion 的 Audio
    组件忽略，音频从 0 秒就播、字幕却按片头时长整体后移，全片错位。
    这里转录成片的真实语音起点，和第一条字幕的时间做比对。
    """
    if not captions:
        return []
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return [_issue(WARN, "字幕同步", "缺 faster-whisper，跳过同步核验")]

    tmp = Path("/tmp/_qc_sync.wav")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "16000", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    if not tmp.exists():
        return [_issue(WARN, "字幕同步", "无法提取音轨")]

    # 不能开 vad_filter：它会剥掉静音段并按剥离后的时间轴报时间戳，
    # 于是片头静音被吃掉、语音起点被算成 0 附近 —— 用它来核对同步会
    # 得出错误结论（这个检查器自己就先踩过一次）。
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(tmp), language="zh", vad_filter=False)
    first = next((s.start for s in segs), None)
    if first is None:
        return [_issue(WARN, "字幕同步", "转录不到语音，无法核验")]

    cap_start = captions[0]["startMs"] / 1000
    drift = abs(cap_start - first)
    if drift > tolerance:
        return [_issue(FAIL, "字幕同步",
                       f"首条字幕 {cap_start:.2f}s，实际语音 {first:.2f}s，偏差 {drift:.2f}s",
                       "片头占用的时间要用真实静音垫进音轨，不能依赖 offsetSeconds")]

    # 只看首条是不够的：整体平移、中途累积的滞后，首条都可能是对的。
    # 漏过一次「全片滞后两句、质检报通过」之后加的这一段 —— 把每条字幕
    # 都对回音轨，取偏差中位数。
    issues = _sync_drift_across_track(tmp, captions, tolerance)
    return issues


def _sync_drift_across_track(audio: Path, captions: list[dict[str, Any]],
                             tolerance: float) -> list[dict[str, str]]:
    """把每条字幕对回音轨，看偏差中位数。"""
    from studio import align
    spoken = "".join(str(c.get("word") or "") for c in captions)
    expected = align.align(spoken, audio, 0.0)
    if not expected or len(expected) != len(captions):
        return []      # 对不上就不下结论，别误报

    import statistics
    deltas = [abs(c["startMs"] - e["startMs"]) / 1000
              for c, e in zip(captions, expected)]
    mid = statistics.median(deltas)
    worst = max(deltas)
    bad = [d for d in deltas if d > tolerance]
    hint = ("字幕时间应从成品音轨转写反推，不要在已含片头静音的音轨上"
            "再加一次 title_offset")

    if mid > tolerance:
        return [_issue(FAIL, "字幕同步",
                       f"全片偏差中位 {mid:.2f}s（最大 {worst:.2f}s）", hint)]
    # 中位数看不见局部滞后：前半对、后半整体差 1.8 秒时中位数仍是 0。
    # 所以再按「超差条数占比」判一次。
    if len(bad) > len(deltas) * 0.25:
        return [_issue(FAIL, "字幕同步",
                       f"{len(bad)}/{len(deltas)} 条字幕偏差超 {tolerance:.1f}s"
                       f"（最大 {worst:.2f}s）", hint)]
    if bad:
        return [_issue(WARN, "字幕同步",
                       f"{len(bad)}/{len(deltas)} 条字幕偏差偏大，最大 {worst:.2f}s")]
    return []


def check_expectations(data: dict[str, Any]) -> list[dict[str, str]]:
    """产出内容是否符合预期 —— 静默降级要被看见。"""
    out: list[dict[str, str]] = []
    if data.get("music") in (None, "", "无"):
        out.append(_issue(WARN, "配乐", "本片没有配乐",
                          "配 PIXABAY_API_KEY 可避免匿名访问被限流"))
    if data.get("footage_used", 0) < data.get("scenes", 0):
        out.append(_issue(WARN, "画面",
                          f"{data.get('scenes')} 镜里只有 {data.get('footage_used')} 镜有画面素材"))
    if not data.get("captions"):
        out.append(_issue(WARN, "字幕", "本片没有字幕"))
    for err in (data.get("generation_errors") or []):
        out.append(_issue(WARN, "生成告警", str(err)[:140]))
    fallbacks = data.get("narrator_fallbacks") or []
    if fallbacks:
        out.append(_issue(WARN, "配音降级",
                          f"首选配音不可用，已降级：{fallbacks[0][:100]}"))
    return out


def inspect(video: Path, data: Optional[dict[str, Any]] = None,
            captions: Optional[list[dict[str, Any]]] = None,
            deep: bool = True,
            narration: Optional[Path] = None,
            expected_lead: float = 0.0) -> dict[str, Any]:
    """跑完整质检，返回问题清单。deep=False 时跳过需要转录的检查。"""
    video = Path(video)
    if not video.exists():
        return {"passed": False, "issues": [_issue(FAIL, "成片", "文件不存在")]}

    issues: list[dict[str, str]] = []
    issues += check_streams(_probe(video))
    issues += check_audio_artifacts(video)
    if data:
        issues += check_expectations(data)
    # 优先用旁白音轨核对同步：成片音轨混了配乐，量不准
    if narration is not None:
        issues += check_narration_lead(Path(narration), expected_lead)
    elif deep and captions:
        issues += check_caption_sync(video, captions)

    return {
        "passed": not any(i["level"] == FAIL for i in issues),
        "fail": sum(1 for i in issues if i["level"] == FAIL),
        "warn": sum(1 for i in issues if i["level"] == WARN),
        "issues": issues,
    }
