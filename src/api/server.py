"""
FastAPI API 서버.

Electron GUI 앱에서 호출하는 HTTP + WebSocket 엔드포인트를 제공한다.
CLI 모드와 독립적으로 동작하며, `python -m src.api.server`로 실행한다.
"""

from __future__ import annotations

import os
import secrets
import socket

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
    # 타이밍 공격 방지 — 상수시간 비교
    if not secrets.compare_digest(authorization[7:], _API_TOKEN):
        raise HTTPException(status_code=403, detail="유효하지 않은 토큰입니다.")


app = FastAPI(
    title="Study Helper API",
    version="1.0.0",
    dependencies=[Depends(_verify_token)],
)

# CORS — localhost만 허용
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 라우트 등록 ───────────────────────────────────────────────
app.include_router(health_routes.router, tags=["health"])
app.include_router(config_routes.router, prefix="/config", tags=["config"])
app.include_router(download_routes.router, prefix="/download", tags=["download"])
app.include_router(notify_routes.router, prefix="/notify", tags=["notify"])


def _find_free_port(preferred: int, host: str = "127.0.0.1", max_tries: int = 10) -> int:
    """preferred 부터 순차적으로 비어있는 포트를 탐색한다.

    Electron 과 연동 시 preferred(기본 18090)가 다른 프로세스에 점유되어 있을 때
    uvicorn 즉시 crash 를 막기 위해 10개 범위 내에서 fallback 포트를 반환한다.
    모두 점유 시 preferred 를 그대로 반환하여 명시적 실패를 유도한다.
    """
    for offset in range(max_tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return candidate
    return preferred


def main():
    """API 서버를 실행한다."""
    preferred_port = int(os.getenv("STUDY_HELPER_API_PORT", "18090"))
    port = _find_free_port(preferred_port)
    if port != preferred_port:
        # Electron 로그에 포트 이관 사실을 표기해 연동 클라이언트가 감지 가능하게 한다.
        print(f"[study-helper] port {preferred_port} busy; using {port}", flush=True)
    uvicorn.run(
        "src.api.server:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
