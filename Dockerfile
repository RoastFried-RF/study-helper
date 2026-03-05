FROM python:3.11-slim

# 시스템 의존성 설치
# - ffmpeg: mp4 → mp3 변환 (사용자 다운로드용)
# - Playwright Chromium 런타임 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv 설치 (빠른 패키지 관리)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 의존성 파일 먼저 복사 (레이어 캐시 활용)
COPY pyproject.toml uv.lock* ./

# torch CPU 전용 설치 — GPU 없는 Docker 환경 최적화
# full torch 대비 이미지 크기 ~1/3 수준으로 감소 (~700MB vs ~2.5GB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# 나머지 패키지 설치 (torch는 위에서 설치했으므로 제외)
RUN uv sync --frozen --no-dev --no-install-package torch

# Playwright 내장 Chromium 설치
# 시스템 Chrome 경로 하드코딩 없이 Playwright가 관리하는 Chromium 사용
RUN uv run playwright install chromium

# 소스 코드 복사
COPY src/ ./src/

# 다운로드 경로 및 캐시 디렉토리 생성
RUN mkdir -p /data/downloads \
    && mkdir -p /root/.cache/whisper \
    && mkdir -p /root/.cache/ms-playwright

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

ENTRYPOINT ["uv", "run", "python", "src/main.py"]
