"""Gemini 原生图像生成工具。

为什么不用项目自带的 google_imagen：
    它调的是 `models/imagen-*:predict`，而 Imagen 系列已对新 Google 账户
    停止开放（返回 404「no longer available to new users」）。Gemini 的
    图像模型走 `generateContent`，同一个 GOOGLE_API_KEY 就能用。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.1-flash-image"


class GeminiImage(BaseTool):
    name = "gemini_image"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "image_generation"
    provider = "google"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    install_instructions = "在「密钥配置」填入 GOOGLE_API_KEY（Google AI Studio 免费申请）"
    capabilities = ["text_to_image"]
    best_for = ["素材库检索不到时的画面兜底", "抽象或概念性画面"]
    not_good_for = ["需要精确还原真实人物或品牌的画面"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string", "default": DEFAULT_MODEL},
            "aspect_ratio": {"type": "string", "default": "16:9"},
            "output_path": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"image": {"type": "string"}}}

    def _key(self) -> str:
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._key() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Gemini 图像按张计费，量级与 Imagen 相当。
        return 0.04

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        key = self._key()
        if not key:
            return ToolResult(success=False, error="未设置 GOOGLE_API_KEY")

        prompt = (inputs.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(success=False, error="prompt 为空")

        model = inputs.get("model") or DEFAULT_MODEL
        ratio = inputs.get("aspect_ratio") or "16:9"
        out_path = Path(inputs.get("output_path") or "gemini_image.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        body = json.dumps({
            "contents": [{"parts": [{
                "text": f"Generate a {ratio} cinematic image. {prompt}"
            }]}]
        }).encode()

        req = urllib.request.Request(f"{API_BASE}/{model}:generateContent",
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-goog-api-key", key)
        # Google 的边缘会拦默认的 Python-urllib UA。
        req.add_header("User-Agent", "AIVideoFactory/1.0")

        try:
            payload = json.loads(urllib.request.urlopen(req, timeout=180).read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", "")
            except Exception:
                pass
            return ToolResult(success=False, error=f"Gemini 图像生成失败 HTTP {exc.code}: {detail[:200]}")
        except Exception as exc:
            return ToolResult(success=False, error=f"Gemini 图像生成失败: {exc}")

        parts = (payload.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        blob = None
        for part in parts:
            data = part.get("inlineData") or part.get("inline_data")
            if data and data.get("data"):
                blob = data["data"]
                break
        if not blob:
            return ToolResult(success=False, error="返回中没有图片数据（可能被安全策略拦截）")

        out_path.write_bytes(base64.b64decode(blob))
        return ToolResult(
            success=True,
            data={"image": str(out_path), "model": model},
            artifacts=[str(out_path)],
            cost_usd=0.04,
            model=model,
            duration_seconds=round(time.time() - started, 1),
        )
