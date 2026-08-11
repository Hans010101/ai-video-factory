"""字幕对齐。

配音改成整镜一次合成之后，句内的时间就没法再靠「每句单独量时长」得到了。
但那个交换是值得的：给 TTS 的文本单位越长，它的语调越自然（实测短语切分
→ 整句 → 整段，音高起伏系数 0.274 → 0.305 → 0.291~0.32），僵硬感主要就
来自把句子切碎了各合成各的。

所以时间轴改为事后从音频里量：用 whisper 转写拿到逐词时间戳，再把它对回
原文的分句上。原文是已知的，whisper 只负责提供「第几个字在第几秒」，
识别错字不影响对齐结果。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# 显示用的分句：按逗号一级的标点断，一条字幕一口气能读完。
_CAPTION_SPLIT = re.compile(r"(?<=[，、,。！？!?；;：:])")


def caption_chunks(text: str) -> list[str]:
    return [c for c in (p.strip() for p in _CAPTION_SPLIT.split(text)) if c]


def _norm(s: str) -> str:
    """只留下参与比对的字符。标点在原文和转写里对不上，一律去掉。"""
    return re.sub(r"[^\w一-鿿]", "", s)


def word_times(audio: Path) -> Optional[list[tuple[str, float, float]]]:
    """转写出逐词时间戳。装不了 whisper 就返回 None，由调用方退回估算。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        # 不能开 vad_filter：它会剥掉静音段并按剥离后的时间轴报时间戳，
        # 对齐结果会整体偏移。
        segs, _ = model.transcribe(str(audio), language="zh",
                                   vad_filter=False, word_timestamps=True)
        out: list[tuple[str, float, float]] = []
        for seg in segs:
            for w in (seg.words or []):
                token = _norm(w.word)
                if token:
                    out.append((token, float(w.start), float(w.end)))
        return out or None
    except Exception:
        return None


def align(text: str, audio: Path, offset_seconds: float = 0.0
          ) -> Optional[list[dict[str, object]]]:
    """把 text 的分句对到 audio 的时间轴上。

    做法是按字符累计走：转写词序列拼起来形成一条字符流，原文每一条字幕
    占其中多少个字符是已知的，于是每条字幕的起止就落在对应词的时间上。
    whisper 少字多字都只会让边界差一两个字，不会累积漂移。
    """
    words = word_times(audio)
    chunks = caption_chunks(text)
    if not words or not chunks:
        return None

    # 每个词覆盖的字符区间
    spans: list[tuple[int, int, float, float]] = []
    pos = 0
    for token, s, e in words:
        spans.append((pos, pos + len(token), s, e))
        pos += len(token)
    total_chars = pos
    if total_chars == 0:
        return None

    plain_len = sum(len(_norm(c)) for c in chunks) or 1
    # 转写与原文的字数总会有出入，按比例缩放到同一把尺子上
    scale = total_chars / plain_len

    def time_at(char_index: float, use_end: bool) -> float:
        target = char_index * scale
        for a, b, s, e in spans:
            if a <= target < b:
                return e if use_end else s
        return spans[-1][3] if use_end else spans[0][2]

    out: list[dict[str, object]] = []
    cursor = 0
    for chunk in chunks:
        n = len(_norm(chunk))
        if n == 0:
            continue
        start = time_at(cursor, False)
        end = time_at(cursor + n - 1, True)
        cursor += n
        if end <= start:                       # 极短词的边界重合，给个下限
            end = start + 0.20
        out.append({"word": chunk,
                    "startMs": int((offset_seconds + start) * 1000),
                    "endMs": int((offset_seconds + end) * 1000)})

    # 相邻字幕不允许交叠，否则播放器会同时显示两条
    for i in range(len(out) - 1):
        if out[i]["endMs"] > out[i + 1]["startMs"]:            # type: ignore[operator]
            out[i]["endMs"] = out[i + 1]["startMs"]            # type: ignore[index]
    return out
