"""Tool catalog: enumerate every registered tool with availability and key needs.

Two kinds of gating exist in the toolset and both must be surfaced:

1. Declared dependencies (`cmd:`, `python:`, `env:`) — checked by
   BaseTool.check_dependencies(), so the failure message is authoritative.
2. Runtime key checks — a tool overrides get_status() and reads
   os.environ.get("SOME_KEY") directly. check_dependencies() passes but the
   tool still reports UNAVAILABLE. The key name only exists in the source, so
   we scan for it.
"""

from __future__ import annotations

import inspect
import re
from functools import lru_cache
from typing import Any

from tools.base_tool import DependencyError
from tools.tool_registry import registry

from studio import i18n

# Matches os.environ.get("X") / os.getenv("X") / os.environ["X"]
_ENV_RE = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z0-9_]+)["']|os\.environ\[\s*["']([A-Z0-9_]+)["']""")

# Env names that are settings, not credentials — never show them as "missing key".
_NON_CREDENTIAL = {
    "PATH", "HOME", "TMPDIR", "OPENMONTAGE_ROOT", "PYTHONPATH",
    "GOOGLE_CLOUD_LOCATION", "KLING_API_BASE_URL", "DOUBAO_SPEECH_VOICE_TYPE",
    "VIDEO_GEN_LOCAL_ENABLED", "VIDEO_GEN_LOCAL_MODEL", "CUDA_VISIBLE_DEVICES",
}

CAPABILITY_LABELS = {
    "video_generation": "AI 视频生成",
    "image_generation": "AI 图像生成",
    "tts": "语音合成",
    "music_generation": "音乐生成",
    "music_search": "音乐检索",
    "music_library": "音乐库",
    "analysis": "分析与理解",
    "video_post": "剪辑与后期",
    "enhancement": "画质增强",
    "avatar": "数字人",
    "character_animation": "角色动画",
    "graphics": "图形与图表",
    "subtitle": "字幕",
    "audio_processing": "音频处理",
    "screen_capture": "屏幕录制",
    "source_ingest": "素材导入",
    "publish": "发布导出",
    "clip_retrieval": "片段检索",
    "clip_acquisition": "片段获取",
    "corpus_population": "语料构建",
    "generic": "其他",
}


def _env_names_in_source(tool: Any) -> list[str]:
    """Extract credential env var names referenced in a tool's module source."""
    try:
        source = inspect.getsource(type(tool))
    except (OSError, TypeError):
        return []
    names = set()
    for a, b in _ENV_RE.findall(source):
        name = a or b
        if name and name not in _NON_CREDENTIAL:
            names.add(name)
    return sorted(names)


def _describe(tool: Any) -> dict[str, Any]:
    declared_env = [d[4:] for d in tool.dependencies if d.startswith("env:")]

    blocked_reason = ""
    try:
        tool.check_dependencies()
        dependency_ok = True
    except DependencyError as exc:
        dependency_ok = False
        blocked_reason = i18n.localize_dependency_error(str(exc))

    # DEGRADED means "runs with reduced capability" (e.g. face_tracker with
    # OpenCV but no MediaPipe) — it is runnable, so treat it as usable and
    # flag it, rather than hiding it alongside genuinely blocked tools.
    status = tool.get_status().value
    degraded = status == "degraded"
    available = status in ("available", "degraded")

    # Keys that would plausibly unlock this tool.
    needs_keys: list[str] = list(declared_env)
    if not available and dependency_ok:
        # Runtime-gated: the key name lives in the source only.
        needs_keys = sorted(set(needs_keys) | set(_env_names_in_source(tool)))
        if needs_keys and not blocked_reason:
            blocked_reason = "需要 API 密钥：" + "、".join(needs_keys)
    if not available and not blocked_reason:
        # check_dependencies() passed and no key was found in source, so the
        # tool overrides get_status() with its own logic — most often a
        # selector with no usable provider, or a local model / external
        # service (ComfyUI, local diffusion weights) that isn't set up.
        blocked_reason = (
            "该工具自行判定不可用：通常是「选择器」类工具尚无任何可用提供商，"
            "或需要本地模型权重 / 外部服务（如 ComfyUI）。配好任一提供商密钥后会自动恢复。"
        )
    if degraded:
        blocked_reason = "降级可用：部分依赖或数据源缺失，核心功能仍可运行。"

    return {
        "name": tool.name,
        "label": i18n.tool_name(tool.name),
        "version": tool.version,
        "status": status,
        "degraded": degraded,
        "capability": tool.capability,
        "capability_label": CAPABILITY_LABELS.get(tool.capability, tool.capability),
        "provider": tool.provider,
        "tier": tool.tier.value,
        "runtime": tool.runtime.value,
        "stability": tool.stability.value,
        "execution_mode": tool.execution_mode.value,
        "available": available,
        "blocked_reason": blocked_reason,
        "needs_keys": needs_keys,
        "dependencies": list(tool.dependencies),
        "install_instructions": tool.install_instructions,
        "input_schema": tool.input_schema or {},
        "best_for": [i18n.localize_phrase(b) for b in tool.best_for],
        "not_good_for": [i18n.localize_phrase(b) for b in tool.not_good_for],
        "capabilities": list(tool.capabilities),
        "cost_hint": "cloud" if tool.runtime.value != "local" else "local",
    }


@lru_cache(maxsize=1)
def _catalog_cached() -> tuple[dict[str, Any], ...]:
    registry.ensure_discovered()
    items = []
    for name in sorted(registry.list_all()):
        tool = registry.get(name)
        if tool is None:
            continue
        try:
            items.append(_describe(tool))
        except Exception as exc:  # never let one bad tool break the console
            items.append({
                "name": name, "capability": "generic", "capability_label": "其他",
                "provider": "unknown", "available": False,
                "blocked_reason": f"元数据读取失败: {exc}", "needs_keys": [],
                "dependencies": [], "install_instructions": "", "input_schema": {},
                "best_for": [], "not_good_for": [], "capabilities": [],
                "tier": "core", "runtime": "local", "stability": "experimental",
                "execution_mode": "sync", "version": "?", "cost_hint": "local",
                "status": "unavailable", "degraded": False, "label": i18n.tool_name(name),
            })
    return tuple(items)


def catalog(refresh: bool = False) -> list[dict[str, Any]]:
    """All tools with availability. Pass refresh=True after changing .env."""
    if refresh:
        _catalog_cached.cache_clear()
        registry.clear()
        registry.discover()
    return [dict(item) for item in _catalog_cached()]


def summary(refresh: bool = False) -> dict[str, Any]:
    """Counts by availability and capability, plus which keys unlock what."""
    items = catalog(refresh=refresh)
    by_capability: dict[str, dict[str, Any]] = {}
    key_unlocks: dict[str, list[str]] = {}

    for item in items:
        bucket = by_capability.setdefault(item["capability"], {
            "capability": item["capability"],
            "label": item["capability_label"],
            "total": 0, "available": 0,
        })
        bucket["total"] += 1
        bucket["available"] += 1 if item["available"] else 0
        if not item["available"]:
            for key in item["needs_keys"]:
                key_unlocks.setdefault(key, []).append(item["name"])

    return {
        "total": len(items),
        "available": sum(1 for i in items if i["available"]),
        "degraded": sum(1 for i in items if i.get("degraded")),
        "blocked": sum(1 for i in items if not i["available"]),
        "needs_key": sum(1 for i in items if not i["available"] and i["needs_keys"]),
        "capabilities": sorted(by_capability.values(), key=lambda b: -b["total"]),
        "key_unlocks": sorted(
            ({"key": k, "unlocks": sorted(v), "count": len(v)} for k, v in key_unlocks.items()),
            key=lambda e: -e["count"],
        ),
    }
