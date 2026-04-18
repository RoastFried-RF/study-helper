FROM python:3.11-slim-bookworm

# 시스템 의존성 설치 및 보안 패치 적용
# - apt-get upgrade: 알려진 CVE (jpeg-xl, freetype, tar 등) 수정
# - ffmpeg: mp4 → mp3 변환 (사용자 다운로드용)
# - curl: HEALTHCHECK 용
# - tini: init process (PID 1) — Playwright 자식 프로세스 좀비 수집
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/*

# TZ=Asia/Seoul 을 compose 없이 docker run 직접 실행 시에도 적용
ENV TZ=Asia/Seoul

# ── 비root 사용자 설정 ──────────────────────────────────────────
# 컨테이너 침해 시 root 권한으로 호스트 볼륨이 변조되거나 다운로드 파일이
# root 소유로 생성되어 Linux 호스트에서 sudo 없이 삭제 불가해지던 문제 방지.
#
# 호스트 사용자 UID/GID 와 일치시키려면 build 시 `--build-arg APP_UID=$(id -u)
# --build-arg APP_GID=$(id -g)` 로 오버라이드. 기본값 1000 은 대부분의
# Linux 배포판 첫 일반 사용자 UID 와 동일.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} appuser \
    && useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash appuser

WORKDIR /app

# uv 설치 (빠른 패키지 관리) — root 권한 필요 단계. 이후 chown 으로 이관.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 의존성 파일 먼저 복사 (레이어 캐시 활용)
COPY pyproject.toml uv.lock* ./

# pip/wheel/setuptools 최신 버전으로 업그레이드 (CVE-2025-8869, CVE-2026-24049 대응)
RUN pip install --no-cache-dir --upgrade pip wheel setuptools

# 패키지 설치
RUN uv sync --frozen --no-dev

# Playwright 캐시를 appuser home 으로 이동. 기존 /root/.cache 경로는
# root 소유라 비-root 실행 시 접근 불가.
ENV PLAYWRIGHT_BROWSERS_PATH=/home/appuser/.cache/ms-playwright
ENV HF_HOME=/home/appuser/.cache/huggingface

# Chrome(H.264 포함)을 우선 설치, ARM64 등 미지원 환경에서는 Chromium으로 fallback.
# Google Chrome은 Linux amd64만 지원 — Apple Silicon(arm64) Docker에서는 Chromium 사용.
# install 은 root 에서 실행 후 cache dir 소유권을 appuser 로 이관.
RUN uv run playwright install --with-deps chrome 2>/dev/null \
    || uv run playwright install --with-deps chromium

# 소스 코드 복사
COPY src/ ./src/
COPY CHANGELOG.md ./

# 다운로드 경로 및 캐시 디렉토리 생성 후 appuser 로 chown
RUN mkdir -p /data/downloads /app/logs \
    && mkdir -p /home/appuser/.cache/huggingface \
    && mkdir -p /home/appuser/.cache/ms-playwright \
    && chown -R appuser:appuser /data /app /home/appuser/.cache

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# API 서버 모드 전용 HEALTHCHECK — CUI 모드에서는 /health 미제공하므로 실패로
# 판정되지만, 이는 컨테이너를 API 서버로 구동하지 않았다는 정보 신호로 충분.
# --start-period=20s 로 Playwright 초기화 시간 확보.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fs http://127.0.0.1:${STUDY_HELPER_API_PORT:-18090}/health || exit 1

# 이후 모든 실행은 appuser 권한으로
USER appuser

# tini 로 Playwright/Chrome 자식 프로세스 signal forwarding + 좀비 수집
ENTRYPOINT ["/usr/bin/tini", "--", "uv", "run", "--no-sync", "python", "src/main.py"]
