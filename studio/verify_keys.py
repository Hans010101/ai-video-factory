"""密钥真实性验证。

「填了」不等于「能用」—— 实际踩过的坑：
    · 密钥有效但没勾对应权限（ElevenLabs 缺 text_to_speech）
    · 把 A 家的密钥填进了 B 家的字段（fal.ai 里填了 sk_ 开头的值）
    · 密钥有效但账单锁死（OpenAI 429）
    · 密钥有效但那个 API 根本不收 API Key（Google Cloud TTS 只认服务账号）

这些都只有真正发一次请求才能发现。所以每家用一个**免费、幂等、不消耗
额度**的轻量端点去探测，而不是只看字符串在不在。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

UA = "AIVideoFactory/1.0"

OK, BAD, WARN, UNKNOWN = "ok", "bad", "warn", "unknown"


def _get(url: str, headers: dict[str, str], timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 上限放宽到 256KB：音色列表这类响应几十 KB，截断会让 JSON 解析失败
            return resp.status, resp.read(262144).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000).decode(errors="replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _msg(body: str, *paths: tuple) -> str:
    """从各家五花八门的错误结构里挖出人话。"""
    try:
        data = json.loads(body)
    except Exception:
        return body[:120]
    for path in paths:
        cur: Any = data
        for step in path:
            if isinstance(cur, dict):
                cur = cur.get(step)
            else:
                cur = None
                break
        if isinstance(cur, str) and cur:
            return cur[:160]
    return body[:120]


# ---- 各家验证器：返回 (状态, 说明) ----

def _elevenlabs(key: str) -> tuple[str, str]:
    code, body = _get("https://api.elevenlabs.io/v1/voices", {"xi-api-key": key})
    if code == 200:
        # 响应体被截断过，不能直接 json.loads —— 200 本身已说明密钥与权限没问题
        try:
            n = len(json.loads(body).get("voices") or [])
            return OK, f"可用，{n} 个音色"
        except Exception:
            return OK, "可用"
    detail = _msg(body, ("detail", "message"), ("detail",))
    if code == 401 and "permission" in detail.lower():
        return WARN, f"密钥有效但权限不足：{detail}"
    if code == 401:
        return BAD, "密钥无效"
    return BAD, f"HTTP {code}: {detail}"


def _openai(key: str) -> tuple[str, str]:
    code, body = _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    if code == 200:
        return OK, "密钥有效"
    detail = _msg(body, ("error", "message"))
    if code == 429:
        return WARN, f"密钥有效但额度受限：{detail}"
    return BAD, f"HTTP {code}: {detail}"


def _google_ai(key: str) -> tuple[str, str]:
    code, body = _get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=1", {})
    if code == 200:
        return OK, "可用（Gemini / Imagen / Veo）"
    return BAD, f"HTTP {code}: {_msg(body, ('error', 'message'))}"


def _pexels(key: str) -> tuple[str, str]:
    code, body = _get("https://api.pexels.com/videos/search?query=city&per_page=1",
                      {"Authorization": key})
    if code == 200:
        return OK, "可用"
    return BAD, f"HTTP {code}: {body[:100]}"


def _pixabay(key: str) -> tuple[str, str]:
    code, body = _get(f"https://pixabay.com/api/?key={urllib.parse.quote(key)}&q=city&per_page=3", {})
    return (OK, "可用") if code == 200 else (BAD, f"HTTP {code}: {body[:100]}")


def _unsplash(key: str) -> tuple[str, str]:
    code, body = _get(f"https://api.unsplash.com/photos/random?client_id={urllib.parse.quote(key)}", {})
    return (OK, "可用") if code == 200 else (BAD, f"HTTP {code}: {body[:100]}")


def _fal(key: str) -> tuple[str, str]:
    if key.startswith("sk_") or ":" not in key:
        return BAD, "格式不对 —— fal.ai 密钥形如 <uuid>:<hex>，不是 sk_ 开头"
    code, body = _get("https://rest.alpha.fal.ai/tokens/", {"Authorization": f"Key {key}"})
    if code in (200, 201, 405):
        return OK, "格式与认证通过"
    if code in (401, 403):
        return BAD, "密钥无效"
    return UNKNOWN, f"无法确认（HTTP {code}）"


def _replicate(key: str) -> tuple[str, str]:
    code, body = _get("https://api.replicate.com/v1/account", {"Authorization": f"Bearer {key}"})
    return (OK, "可用") if code == 200 else (BAD, f"HTTP {code}: {body[:100]}")


def _hf(key: str) -> tuple[str, str]:
    code, body = _get("https://huggingface.co/api/whoami-v2", {"Authorization": f"Bearer {key}"})
    return (OK, "可用") if code == 200 else (BAD, f"HTTP {code}: {body[:100]}")


def _google_sa(path: str) -> tuple[str, str]:
    if not os.path.exists(path):
        return BAD, "文件不存在，需填服务账号 JSON 的绝对路径"
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception as exc:
        return BAD, f"不是合法 JSON：{exc}"
    if data.get("type") != "service_account":
        return BAD, "不是服务账号密钥文件"
    return OK, f"服务账号 {data.get('client_email', '')[:40]}"


VERIFIERS: dict[str, Callable[[str], tuple[str, str]]] = {
    "ELEVENLABS_API_KEY": _elevenlabs,
    "OPENAI_API_KEY": _openai,
    "GOOGLE_API_KEY": _google_ai,
    "GEMINI_API_KEY": _google_ai,
    "PEXELS_API_KEY": _pexels,
    "PIXABAY_API_KEY": _pixabay,
    "UNSPLASH_ACCESS_KEY": _unsplash,
    "FAL_KEY": _fal,
    "FAL_AI_API_KEY": _fal,
    "REPLICATE_API_TOKEN": _replicate,
    "HF_TOKEN": _hf,
    "GOOGLE_APPLICATION_CREDENTIALS": _google_sa,
}

# 这些 API 没有免费的探测端点，或验证本身会消耗额度/产生费用
NO_PROBE_NOTE = "该服务没有免费的探测接口，只能在实际调用时确认"


def verify_one(key_name: str, value: str) -> dict[str, Any]:
    if not value:
        return {"key": key_name, "state": UNKNOWN, "detail": "未配置"}
    fn = VERIFIERS.get(key_name)
    if fn is None:
        return {"key": key_name, "state": UNKNOWN, "detail": NO_PROBE_NOTE}
    try:
        state, detail = fn(value)
    except Exception as exc:
        state, detail = UNKNOWN, f"验证异常：{type(exc).__name__}"
    return {"key": key_name, "state": state, "detail": detail}


def verify_all(values: dict[str, str], only: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """并发验证，逐个串行会等很久。"""
    targets = [(k, v) for k, v in values.items()
               if v and (not only or k in only)]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda kv: verify_one(*kv), targets))
