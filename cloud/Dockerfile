# AI 视频工厂 · 云端渲染容器
#
# 跑的就是本地那套 studio.agent —— 它本来就是「主动拉取任务」的模式，
# 塞进容器不需要改架构：容器启动后轮询 Worker 认领订单，本地算力换成
# 云端算力，产物照样回传 R2。
#
# 刻意不装 torch / transformers / rembg（那些是本地增强工具，约 2GB），
# 出片主链路只需要 FFmpeg + Node/Remotion + Piper + 核心 Python 依赖。

FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg：编码、拼接、探测时长
# chromium 依赖：Remotion 渲染要跑无头浏览器
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv ca-certificates curl \
      ffmpeg \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
      libpango-1.0-0 libcairo2 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Python 依赖（先装依赖，代码后拷，改代码不必重装）----
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt \
    && /opt/venv/bin/pip install piper-tts certifi
ENV PATH="/opt/venv/bin:${PATH}"

# ---- Remotion 依赖 ----
COPY remotion-composer/package.json remotion-composer/package-lock.json* remotion-composer/
RUN cd remotion-composer && npm ci --omit=dev 2>/dev/null || npm install --omit=dev

# ---- Piper 中英文语音模型 ----
# 装进镜像而不是启动时下载：容器按需唤醒，每次冷启都下 60MB 不现实
RUN mkdir -p /root/.piper/models \
    && SSL_CERT_FILE=$(/opt/venv/bin/python -c "import certifi;print(certifi.where())") \
       /opt/venv/bin/python -m piper.download_voices \
       zh_CN-huayan-medium --download-dir /root/.piper/models \
    && SSL_CERT_FILE=$(/opt/venv/bin/python -c "import certifi;print(certifi.where())") \
       /opt/venv/bin/python -m piper.download_voices \
       en_US-lessac-medium --download-dir /root/.piper/models

# ---- 项目代码 ----
COPY lib/ lib/
COPY tools/ tools/
COPY studio/ studio/
COPY remotion-composer/src/ remotion-composer/src/
COPY remotion-composer/public/ remotion-composer/public/
COPY schemas/ schemas/
COPY config.yaml ./

# Remotion 需要可写的 public 目录放素材与旁白
RUN mkdir -p remotion-composer/public/studio projects/studio

# 容器内没有 .env —— 密钥通过 Cloudflare Secrets 注入为环境变量
ENV PYTHONPATH=/app \
    AVF_AGENT_NAME=cloudflare-container

CMD ["/opt/venv/bin/python", "-m", "studio.agent", \
     "--url", "https://ai-video-factory.hans-pan007.workers.dev", \
     "--name", "cloudflare-container"]
