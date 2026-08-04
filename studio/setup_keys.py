"""交互式密钥配置：逐个提示输入，写入本机 .env。

    .venv/bin/python -m studio.setup_keys

输入用 getpass 隐藏回显，密钥不会出现在终端回滚区或 shell 历史里。
直接回车 = 跳过该项；已配置的项回车 = 保持不变。

写完会重新计算工具可用性，直接告诉你这一轮解锁了哪些工具。
"""

from __future__ import annotations

import argparse
import os
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 按「解锁价值 / 获取成本」排序，逐个提示
KEY_ORDER: list[tuple[str, str, str, str]] = [
    ("PEXELS_API_KEY", "Pexels 素材库",
     "高质量策展视频与图片，画面质量的关键", "完全免费"),
    ("GOOGLE_API_KEY", "Google AI",
     "一个键解锁 Gemini 图像 + TTS + Veo 视频 + Lyria 音乐", "有免费额度"),
    ("ELEVENLABS_API_KEY", "ElevenLabs",
     "高质量配音与音效生成", "有免费额度"),
    ("OPENAI_API_KEY", "OpenAI",
     "Sora 2 视频生成、TTS 配音、图像生成", "按量付费"),
]


def _prepare_env() -> None:
    extra = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join([p for p in extra if p and p not in current] + current)
    import ssl
    if not ssl.get_default_verify_paths().cafile:
        try:
            import certifi
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
            os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
        except ImportError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(prog="studio.setup_keys", description="逐个配置 API 密钥")
    parser.add_argument("--only", nargs="*", help="只配置指定的密钥名")
    args = parser.parse_args()

    _prepare_env()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    from lib.env_loader import load_env
    try:
        load_env()
    except Exception:
        pass

    from studio import catalog as catalog_mod
    from studio import env_manager

    before = catalog_mod.summary()
    before_names = {t["name"] for t in catalog_mod.catalog() if t["available"]}

    existing = env_manager.read_env()
    targets = [k for k in KEY_ORDER if not args.only or k[0] in args.only]

    print("=" * 64)
    print("  AI 视频工厂 · 密钥配置")
    print(f"  当前可用工具 {before['available']}/{before['total']}")
    print()
    print("  · 输入不会显示在屏幕上，也不会进入 shell 历史")
    print("  · 直接回车 = 跳过（已配置的保持不变）")
    print("  · 密钥只写入本机 .env，不会上传到任何地方")
    print("=" * 64)

    updates: dict[str, str] = {}

    for i, (key, label, desc, tier) in enumerate(targets, 1):
        already = bool(existing.get(key) or os.environ.get(key))
        print()
        print(f"[{i}/{len(targets)}] {label}   （{tier}）")
        print(f"    {desc}")
        print(f"    变量名：{key}" + ("   ✅ 已配置，回车保持不变" if already else ""))
        try:
            value = getpass("    粘贴密钥后回车 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n已取消，未做任何修改。")
            return 1
        if not value:
            print("    → 跳过")
            continue
        updates[key] = value
        print(f"    → 已记录（{len(value)} 位）")

    if not updates:
        print("\n没有需要写入的密钥，退出。")
        return 0

    print()
    print("正在写入 .env 并重新检测工具可用性…")
    env_manager.write_keys(updates)

    after = catalog_mod.summary(refresh=True)
    after_names = {t["name"] for t in catalog_mod.catalog() if t["available"]}
    unlocked = sorted(after_names - before_names)

    from studio import i18n

    print()
    print("=" * 64)
    print(f"  已写入 {len(updates)} 个密钥：{'、'.join(sorted(updates))}")
    print(f"  可用工具 {before['available']} → {after['available']} （共 {after['total']}）")
    if unlocked:
        print(f"  本轮解锁 {len(unlocked)} 个：")
        for name in unlocked:
            print(f"    + {i18n.tool_name(name)}  ({name})")
    else:
        print("  没有新增可用工具 —— 密钥可能无效，或对应工具还缺其他依赖。")
    print("=" * 64)
    print()
    print("工作台如果正在运行，刷新页面即可看到新状态：http://127.0.0.1:8760")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
