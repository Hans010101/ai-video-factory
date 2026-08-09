"""把中文画面提示词转成英文检索词。

素材库（Pexels、Archive.org、Wikimedia）的标签与描述几乎全是英文，中文
查询虽然偶尔也能命中，但精准度明显更低。检索前先转成英文能显著提升命中
质量。

只影响「检索用的关键词」，不改动脚本本身，也不影响旁白与字幕。
翻译失败时原样返回 —— 宁可用中文查一次，也不能让整条流程断掉。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODELS = ("gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite")

_CJK = re.compile(r"[一-鿿]")
_cache: dict[str, str] = {}

PROMPT = (
    "Convert this Chinese shot description into a short English stock-footage "
    "search query. Output ONLY the query: 3-8 concrete visual keywords "
    "(subject, setting, lighting, mood). No quotes, no explanation, no "
    "punctuation beyond spaces. Drop abstract or narrative words that stock "
    "libraries cannot match.\n\n"
)


def has_chinese(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def to_english(text: str, timeout: int = 30) -> str:
    """中文 → 英文检索词。非中文、无密钥或调用失败时原样返回。"""
    text = (text or "").strip()
    if not text or not has_chinese(text):
        return text
    if text in _cache:
        return _cache[text]

    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        return text

    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT + text[:600]}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 15000},
    }).encode()

    for model in MODELS:
        req = urllib.request.Request(f"{API_BASE}/{model}:generateContent",
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-goog-api-key", key)
        # Google 边缘会拦默认的 Python-urllib UA
        req.add_header("User-Agent", "AIVideoFactory/1.0")
        try:
            payload = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        except Exception:
            continue

        parts = (payload.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        out = " ".join(p.get("text", "") for p in parts).strip()
        # 模型偶尔会加引号或换行，清理掉
        out = re.sub(r"[\"'`\n\r]+", " ", out).strip()
        out = re.sub(r"\s{2,}", " ", out)
        if out and not has_chinese(out):
            _cache[text] = out
            return out

    return text
