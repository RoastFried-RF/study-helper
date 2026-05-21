"""FastAPI API 레이어 회귀 테스트.

`docs/process-logic-review.md` 의 API-F1~F3 finding + SEC-002 토큰 인증
하드닝을 보호한다. `tests/test_api*.py` 신규 — 기존 API 테스트 0개.

핵심 주의사항:
- `src/api/server.py` 는 **import 시점**에 `STUDY_HELPER_API_TOKEN` 미설정이면
  `RuntimeError` 를 던진다(SEC-002 fail-closed). 따라서 모든 fixture 는
  env 를 먼저 설정한 뒤 `importlib.reload` 로 server 모듈을 재로딩한다.
- 외부 의존(ffmpeg=`convert_to_mp3`, telegram, whisper)은 mock.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# FastAPI TestClient 는 httpx 를 요구한다. Docker 컨테이너에는 설치돼 있으나
# 호스트 환경에 없을 수 있어 graceful skip.
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

_TEST_TOKEN = "test-secret-token-abcdef0123456789"


def _reload_server(monkeypatch, *, token: str | None, allow_no_token: bool = False):
    """env 를 설정하고 src.api.server (+ 라우트 모듈) 를 재로딩해 app 을 반환한다.

    server.py 의 module-level `_API_TOKEN` / `_ALLOW_NO_TOKEN` 가 import 시점에
    고정되므로, env 변경을 반영하려면 반드시 reload 해야 한다.
    """
    if token is None:
        monkeypatch.delenv("STUDY_HELPER_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("STUDY_HELPER_API_TOKEN", token)
    if allow_no_token:
        monkeypatch.setenv("STUDY_HELPER_API_ALLOW_NO_TOKEN", "1")
    else:
        monkeypatch.delenv("STUDY_HELPER_API_ALLOW_NO_TOKEN", raising=False)

    # 라우트 모듈도 함께 재로딩해 server.py 가 fresh 한 router 객체를 include 하도록 한다.
    for mod in [
        "src.api.server",
        "src.api.routes.download",
        "src.api.routes.config",
        "src.api.routes.notify",
        "src.api.routes.health",
    ]:
        if mod in sys.modules:
            del sys.modules[mod]

    server = importlib.import_module("src.api.server")
    return server


@pytest.fixture
def authed_client(monkeypatch):
    """토큰이 설정된 정상 서버의 TestClient."""
    server = _reload_server(monkeypatch, token=_TEST_TOKEN)
    with TestClient(server.app) as client:
        yield client


def _auth_header(token: str = _TEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────
# SEC-002: 토큰 인증 fail-closed
# ─────────────────────────────────────────────────────────────────────


def test_server_boot_refused_without_token(monkeypatch):
    """SEC-002: STUDY_HELPER_API_TOKEN 미설정 + 우회 플래그 없음 → import 시 RuntimeError."""
    with pytest.raises(RuntimeError, match="STUDY_HELPER_API_TOKEN"):
        _reload_server(monkeypatch, token=None, allow_no_token=False)


def test_server_boots_with_allow_no_token_flag(monkeypatch):
    """SEC-002: 개발용 우회 플래그(STUDY_HELPER_API_ALLOW_NO_TOKEN=1) 설정 시 토큰 없이 부팅 허용."""
    server = _reload_server(monkeypatch, token=None, allow_no_token=True)
    assert server.app is not None
    # 우회 모드에서는 인증 의존성이 통과돼야 한다.
    with TestClient(server.app) as client:
        resp = client.get("/config/credentials")
        assert resp.status_code == 200


def test_protected_route_without_auth_header_returns_401(authed_client):
    """SEC-002: Authorization 헤더 없이 보호 라우트 호출 → 401."""
    resp = authed_client.get("/config/credentials")
    assert resp.status_code == 401


def test_protected_route_with_wrong_token_returns_403(authed_client):
    """SEC-002: 잘못된 Bearer 토큰 → 403."""
    resp = authed_client.get(
        "/config/credentials", headers=_auth_header("wrong-token-value")
    )
    assert resp.status_code == 403


def test_protected_route_with_malformed_authorization_returns_401(authed_client):
    """SEC-002: 'Bearer ' 접두사 없는 Authorization 헤더 → 401."""
    resp = authed_client.get(
        "/config/credentials", headers={"Authorization": _TEST_TOKEN}
    )
    assert resp.status_code == 401


def test_protected_route_with_correct_token_passes(authed_client):
    """SEC-002: 정확한 토큰 → 인증 통과 (200)."""
    resp = authed_client.get("/config/credentials", headers=_auth_header())
    assert resp.status_code == 200
    assert "has_credentials" in resp.json()


def test_health_endpoint_is_public(authed_client):
    """SEC-002: /health 는 무인증 공개 엔드포인트 — 토큰 없이 200."""
    resp = authed_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version_endpoint_is_public(authed_client):
    """SEC-002 + health/version: /version 은 무인증 공개 엔드포인트."""
    resp = authed_client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert "version" in body
    assert isinstance(body["version"], str)
    assert body["version"]  # 비어있지 않음


# ─────────────────────────────────────────────────────────────────────
# API-F3: /convert 변환 실패 시 ffmpeg stderr/경로 미노출 — 고정 코드만
# ─────────────────────────────────────────────────────────────────────


def test_convert_failure_returns_fixed_code_no_stderr_leak(authed_client, tmp_path, monkeypatch):
    """API-F3: convert_to_mp3 가 RuntimeError(ffmpeg stderr/경로 포함)를 던져도
    응답에는 'CONVERT_FAILED' 고정 코드만 노출되고 stderr/경로는 새지 않는다.
    """
    from src.config import Config

    # 경로 검증 통과를 위해 다운로드 디렉토리를 tmp_path 로 고정하고 mp4 를 생성.
    monkeypatch.setattr(Config, "get_download_dir", staticmethod(lambda: str(tmp_path)))
    mp4 = tmp_path / "lecture.mp4"
    mp4.write_bytes(b"fake mp4")

    secret_stderr = "ffmpeg: /data/downloads/secret_path/lecture.mp4 codec error TRACEBACK"

    def _boom(_path):
        raise RuntimeError(secret_stderr)

    # download 라우트 모듈이 import 한 convert_to_mp3 심볼을 직접 패치.
    import src.api.routes.download as download_mod

    monkeypatch.setattr(download_mod, "convert_to_mp3", _boom)

    resp = authed_client.post(
        "/download/convert",
        json={"mp4_path": str(mp4), "delete_original": False},
        headers=_auth_header(),
    )

    assert resp.status_code == 500
    body = resp.json()
    # 고정 코드만 노출.
    assert body.get("detail") == "CONVERT_FAILED"
    # ffmpeg stderr / 파일 경로 / traceback 이 응답 어디에도 새지 않아야 한다.
    raw = resp.text
    assert "TRACEBACK" not in raw, f"ffmpeg stderr 누출: {raw}"
    assert "secret_path" not in raw, f"파일 경로 누출: {raw}"
    assert "codec error" not in raw


def test_convert_rejects_path_outside_download_dir(authed_client, tmp_path, monkeypatch):
    """경로 검증: 다운로드 디렉토리 밖 경로는 400 (CONVERT 단계 진입 전 차단)."""
    from src.config import Config

    monkeypatch.setattr(Config, "get_download_dir", staticmethod(lambda: str(tmp_path)))
    resp = authed_client.post(
        "/download/convert",
        json={"mp4_path": "/etc/passwd", "delete_original": False},
        headers=_auth_header(),
    )
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────
# API-F1: WS /pipeline 인증 단계 비-JSON 입력 → INVALID_AUTH_MESSAGE + close(4003)
# ─────────────────────────────────────────────────────────────────────


def test_ws_pipeline_non_json_auth_message(authed_client):
    """API-F1: 인증 첫 메시지가 비-JSON(텍스트)이면 'INVALID_AUTH_MESSAGE' +
    close code 4003. PIPELINE_ERROR 로 오표기되면 안 된다.
    """
    with authed_client.websocket_connect("/download/pipeline") as ws:
        # 비-JSON 텍스트를 인증 메시지 자리에 전송 → receive_json() 이 ValueError.
        ws.send_text("this-is-not-json{{{")
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "INVALID_AUTH_MESSAGE", (
            f"비-JSON 인증 입력이 잘못 분류됨: {msg}"
        )
        # PIPELINE_ERROR 로 새지 않았는지 명시 확인.
        assert msg["message"] != "PIPELINE_ERROR"
        # 서버가 close(4003) 했으므로 추가 수신 시 WebSocketDisconnect(code=4003).
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4003


def test_ws_pipeline_non_dict_json_auth_message(authed_client):
    """API-F1 보강: 인증 메시지가 JSON 이지만 dict 가 아닌 경우(배열 등)도
    INVALID_AUTH_MESSAGE + close(4003).
    """
    with authed_client.websocket_connect("/download/pipeline") as ws:
        ws.send_json(["not", "a", "dict"])
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "INVALID_AUTH_MESSAGE"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4003


def test_ws_pipeline_wrong_token_closes_4003(authed_client):
    """WS 인증: 잘못된 토큰 → '인증 실패' + close(4003)."""
    with authed_client.websocket_connect("/download/pipeline") as ws:
        ws.send_json({"token": "wrong-token"})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "인증 실패"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4003


# ─────────────────────────────────────────────────────────────────────
# API-F2: WS /pipeline base_dir 밖 경로 → PATH_INVALID + close(4003)
#         (PIPELINE_ERROR 로 오표기 안 됨)
# ─────────────────────────────────────────────────────────────────────


def test_ws_pipeline_path_outside_base_dir_returns_path_invalid(
    authed_client, tmp_path, monkeypatch
):
    """API-F2: 인증 통과 후 base_dir 밖 mp4_path → 'PATH_INVALID' + close(4003).
    HTTPException 이 WS 컨텍스트에서 PIPELINE_ERROR 로 오표기되면 안 된다.
    """
    from src.config import Config

    monkeypatch.setattr(Config, "get_download_dir", staticmethod(lambda: str(tmp_path)))

    with authed_client.websocket_connect("/download/pipeline") as ws:
        # 1) 정상 토큰으로 인증 통과
        ws.send_json({"token": _TEST_TOKEN})
        # 2) base_dir 밖 경로를 가진 PipelineRequest 전송
        ws.send_json(
            {
                "mp4_path": "/etc/shadow",
                "course_name": "테스트과목",
                "week_label": "1주차",
                "lecture_title": "강의1",
            }
        )
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "PATH_INVALID", (
            f"base_dir 밖 경로가 PATH_INVALID 로 분류되지 않음 (오표기): {msg}"
        )
        # PIPELINE_ERROR 로 새지 않았는지 명시 확인 (API-F2 핵심).
        assert msg["message"] != "PIPELINE_ERROR"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4003
