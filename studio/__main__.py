"""Launch the Studio console: python -m studio

Binds to 127.0.0.1 by design — the console executes tools and edits .env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _prepare_path() -> None:
    """Put .venv/bin and Homebrew on PATH so `piper`/`ffmpeg` resolve.

    Tools shell out with subprocess, which inherits this process's PATH. When
    the server is started as `.venv/bin/python -m studio`, .venv/bin is NOT on
    PATH automatically, so piper_tts would report itself unavailable.
    """
    extra = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [p for p in extra if p and p not in current] + current
    )


def _prepare_tls() -> None:
    """Point OpenSSL at certifi's CA bundle when the interpreter has none.

    python.org builds on macOS ship without a CA store unless the user runs
    "Install Certificates.command", so ssl.get_default_verify_paths().cafile
    is None and every HTTPS call fails with CERTIFICATE_VERIFY_FAILED. That
    breaks model downloads and every cloud provider tool. Setting these two
    variables fixes urllib, requests and httpx in one shot.
    """
    import ssl

    if ssl.get_default_verify_paths().cafile:
        return
    try:
        import certifi
    except ImportError:
        return
    bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def main() -> int:
    parser = argparse.ArgumentParser(prog="studio", description="AI 视频工厂 · 本地工作台")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workers", type=int, default=3, help="并发执行的任务数")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    _prepare_path()
    _prepare_tls()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    from lib.env_loader import load_env  # noqa: E402
    try:
        load_env()
    except Exception:
        pass

    from studio.jobs import QUEUE  # noqa: E402
    QUEUE.workers = args.workers

    import uvicorn  # noqa: E402

    url = f"http://{args.host}:{args.port}"
    print("=" * 58)
    print("  AI 视频工厂 · 工作台")
    print(f"  {url}")
    print(f"  并发: {args.workers}   根目录: {ROOT}")
    print("=" * 58)

    if args.open:
        import threading, webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run("studio.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
