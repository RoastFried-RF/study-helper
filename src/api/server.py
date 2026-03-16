"""
FastAPI API 서버.

Electron GUI 앱에서 호출하는 HTTP + WebSocket 엔드포인트를 제공한다.
CLI 모드와 독립적으로 동작하며, `python -m src.api.server`로 실행한다.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import config as config_routes
from src.api.routes import download as download_routes
from src.api.routes import health as health_routes
from src.api.routes import notify as notify_routes

# ── 토큰 인증 ─────────────────────────────────────────────────
# Electron이 시작 시 랜덤 토큰을 생성하여 STUDY_HELPER_API_TOKEN 환경변수로 전달한다.
# 토큰이 설정되지 않으면 인증 없이 동작한다 (개발 모드).
_API_TOKEN = os.getenv("STUDY_HELPER_API_TOKEN", "")


def _verify_token(authorization: str | None = Header(default=None)):
    """Bearer 토큰 인증. 토큰 미설정 시 localhost 직접 접근만 허용."""
    if not _API_TOKEN:
        # 토큰 미설정(개발 모드)에서도 localhost만 허용
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")
    if authorization[7:] != _API_TOKEN:
        raise HTTPException(status_code=403, detail="유효하지 않은 토큰입니다.")


app = FastAPI(
    title="Study Helper API",
    version="1.0.0",
    dependencies=[Depends(_verify_token)],
)

# CORS — localhost만 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # localhost 전용 서버이므로 모든 origin 허용
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 라우트 등록 ───────────────────────────────────────────────
app.include_router(health_routes.router, tags=["health"])
app.include_router(config_routes.router, prefix="/config", tags=["config"])
app.include_router(download_routes.router, prefix="/download", tags=["download"])
app.include_router(notify_routes.router, prefix="/notify", tags=["notify"])


def main():
    """API 서버를 실행한다."""
    port = int(os.getenv("STUDY_HELPER_API_PORT", "18090"))
    uvicorn.run(
        "src.api.server:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
