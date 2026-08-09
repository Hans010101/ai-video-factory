"""Read/write the local .env so keys can be managed from the console.

Values are never sent back to the browser — the API reports only whether a
key is set and a masked preview. Writes preserve comments and key order.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

_LINE_RE = re.compile(r"^([A-Z0-9_]+)=(.*)$")

# Where to get each key, so the console can link straight to the signup page.
KEY_INFO: dict[str, dict[str, str]] = {
    "GOOGLE_API_KEY": {"label": "Google AI (Gemini/Imagen/Veo/Lyria)", "url": "https://aistudio.google.com/apikey", "tier": "免费额度"},
    "GEMINI_API_KEY": {"label": "Gemini API", "url": "https://aistudio.google.com/apikey", "tier": "免费额度"},
    "OPENAI_API_KEY": {"label": "OpenAI (Sora 2 / TTS / 图像)", "url": "https://platform.openai.com/api-keys", "tier": "按量付费"},
    "ELEVENLABS_API_KEY": {"label": "ElevenLabs 配音与音乐", "url": "https://elevenlabs.io/app/settings/api-keys", "tier": "免费额度"},
    "PEXELS_API_KEY": {"label": "Pexels 免费素材库", "url": "https://www.pexels.com/api/new/", "tier": "完全免费"},
    "PIXABAY_API_KEY": {"label": "Pixabay 免费素材库", "url": "https://pixabay.com/api/docs/", "tier": "完全免费"},
    "UNSPLASH_ACCESS_KEY": {"label": "Unsplash 免费图库", "url": "https://unsplash.com/oauth/applications", "tier": "完全免费"},
    "FAL_KEY": {"label": "fal.ai 图像/视频网关", "url": "https://fal.ai/dashboard/keys", "tier": "按量付费"},
    "FAL_AI_API_KEY": {"label": "fal.ai（同 FAL_KEY）", "url": "https://fal.ai/dashboard/keys", "tier": "按量付费"},
    "REPLICATE_API_TOKEN": {"label": "Replicate 模型托管", "url": "https://replicate.com/account/api-tokens", "tier": "按量付费"},
    "KLING_API_KEY": {"label": "可灵 Kling 官方 API", "url": "https://app.klingai.com/", "tier": "按量付费"},
    "DASHSCOPE_API_KEY": {"label": "阿里云百炼 DashScope", "url": "https://bailian.console.aliyun.com/", "tier": "免费额度"},
    "SUNO_API_KEY": {"label": "Suno 音乐生成", "url": "https://suno.com/", "tier": "按量付费"},
    "HEYGEN_API_KEY": {"label": "HeyGen 数字人", "url": "https://app.heygen.com/settings", "tier": "按量付费"},
    "RUNWAY_API_KEY": {"label": "Runway 视频生成", "url": "https://dev.runwayml.com/", "tier": "按量付费"},
    "XAI_API_KEY": {"label": "xAI Grok 图像/视频", "url": "https://console.x.ai/", "tier": "按量付费"},
    "HF_TOKEN": {"label": "Hugging Face", "url": "https://huggingface.co/settings/tokens", "tier": "免费"},
    "HIGGSFIELD_API_KEY": {"label": "Higgsfield 视频", "url": "https://higgsfield.ai/", "tier": "按量付费"},
    "AZURE_SPEECH_KEY": {"label": "Azure 语音服务", "url": "https://portal.azure.com/", "tier": "免费额度"},
}


# 重要性分级：数字越小越靠前。依据是「对成片质量的直接影响 × 获取成本」。
PRIORITY: dict[str, int] = {
    # 1 — 核心：直接决定成片质量，且免费或有免费额度
    "ELEVENLABS_API_KEY": 10,   # 配音，中文自然度决定观感
    "PEXELS_API_KEY": 11,       # 画面素材，完全免费
    "GOOGLE_API_KEY": 12,       # 图像 + 视频 + 中文转英文检索
    # 2 — 扩展：明显增强能力
    "OPENAI_API_KEY": 20,
    "FAL_KEY": 21,
    "PIXABAY_API_KEY": 22,
    "UNSPLASH_ACCESS_KEY": 23,
    "GEMINI_API_KEY": 24,
    "REPLICATE_API_TOKEN": 25,
    # 3 — 特定场景：某类片子才用得上
    "KLING_API_KEY": 30, "RUNWAY_API_KEY": 31, "HEYGEN_API_KEY": 32,
    "SUNO_API_KEY": 33, "DASHSCOPE_API_KEY": 34, "XAI_API_KEY": 35,
    "HIGGSFIELD_API_KEY": 36, "HIGGSFIELD_API_SECRET": 37,
    "AZURE_SPEECH_KEY": 38, "DOUBAO_SPEECH_API_KEY": 39,
    "HF_TOKEN": 40, "VOLC_ACCESSKEY": 41, "VOLC_SECRETKEY": 42,
    "FAL_AI_API_KEY": 43,
    "GOOGLE_APPLICATION_CREDENTIALS": 44,
}

# 这些不是密钥，是配套参数（区域、项目名、端点、本地模型开关）。
# 和密钥混排是界面显得杂乱的主因，单独归组放到最后。
SETTING_KEYS = {
    "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_PROJECT", "KLING_API_BASE_URL",
    "AZURE_SPEECH_REGION", "DOUBAO_SPEECH_VOICE_TYPE", "MODAL_LTX2_ENDPOINT_URL",
    "VIDEO_GEN_LOCAL_ENABLED", "VIDEO_GEN_LOCAL_MODEL",
}

DEFAULT_PRIORITY = 50


def _mask(value: str) -> str:
    v = value.strip().strip('"').strip("'")
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:4]}{'•' * 6}{v[-4:]}"


def read_env() -> dict[str, str]:
    """Raw key -> value from .env (never leaves the server)."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def known_keys() -> list[str]:
    """Every key name the project understands, from .env.example."""
    names: list[str] = []
    if ENV_EXAMPLE.exists():
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            m = _LINE_RE.match(line.strip())
            if m:
                names.append(m.group(1))
    for k in KEY_INFO:
        if k not in names:
            names.append(k)
    return names


def status(unlock_map: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Per-key state for the console — masked values only."""
    current = read_env()
    unlock_map = unlock_map or {}
    rows = []
    for name in known_keys():
        value = current.get(name, "") or os.environ.get(name, "")
        info = KEY_INFO.get(name, {})
        is_setting = name in SETTING_KEYS
        rows.append({
            "key": name,
            "set": bool(value),
            "masked": _mask(value),
            "label": info.get("label", ""),
            "url": info.get("url", ""),
            "tier": info.get("tier", ""),
            "unlocks": unlock_map.get(name, []),
            "unlock_count": len(unlock_map.get(name, [])),
            "group": "setting" if is_setting else "credential",
            "priority": PRIORITY.get(name, DEFAULT_PRIORITY),
        })

    # 已配置的排前面（not set → False 排前），组内再按重要性，最后按名字。
    # 配套参数整体沉底，不与密钥混排。
    rows.sort(key=lambda r: (
        r["group"] == "setting",
        not r["set"],
        r["priority"],
        r["key"],
    ))
    return rows


def write_keys(updates: dict[str, str]) -> dict[str, Any]:
    """Set or clear keys in .env, preserving comments and ordering.

    An empty string clears the key. Values are also pushed into os.environ so
    tool availability refreshes without restarting the server.
    """
    updates = {k.strip().upper(): v.strip() for k, v in updates.items() if k.strip()}
    if not updates:
        return {"written": 0, "keys": []}

    if not ENV_PATH.exists():
        if ENV_EXAMPLE.exists():
            ENV_PATH.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text("", encoding="utf-8")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()

    for i, line in enumerate(lines):
        m = _LINE_RE.match(line.strip())
        if m and m.group(1) in updates:
            name = m.group(1)
            lines[i] = f"{name}={updates[name]}"
            seen.add(name)

    for name, value in updates.items():
        if name not in seen:
            lines.append(f"{name}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for name, value in updates.items():
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    return {"written": len(updates), "keys": sorted(updates)}
