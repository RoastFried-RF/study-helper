"""다운로드 결과 데이터클래스와 실패 사유 상수.

run_download 및 관련 레이어가 성공/실패 상태를 구조화된 형태로 반환하기 위해 사용한다.
사유 상수는 study_helper.log와 auto_progress.json에 그대로 기록되므로
값을 바꿀 때는 로그 파서/복구 스크립트 호환성을 함께 점검할 것.
"""

from dataclasses import dataclass
from pathlib import Path

# ── 다운로드 실패 사유 ─────────────────────────────────────
REASON_UNSUPPORTED = "unsupported"                # learningx 등 구조적 다운로드 불가
REASON_URL_EXTRACT_FAILED = "url_extract_failed"  # mp4 URL 추출 실패 (HLS 전용 플레이어 등)
REASON_SSRF_BLOCKED = "ssrf_blocked"              # 허용 호스트/프로토콜 위반
REASON_NETWORK = "network"                        # 타임아웃, 연결 오류, 청크 손상
REASON_PATH_INVALID = "path_invalid"              # base_dir 벗어난 경로
REASON_MP3_FAILED = "mp3_convert_failed"          # ffmpeg 변환 실패
REASON_SUSPICIOUS_STUB = "suspicious_stub"        # 파일 크기/시그니처가 가짜 파일 의심
REASON_UNKNOWN = "unknown"                        # 분류 불가

# ── URL 추출 실패 세분화 (REASON_URL_EXTRACT_FAILED 의 sub-reason) ────
# Phase 2: url_extract_failed 단일 reason 으로 뭉뚱그려지던 문제를 해결하기
# 위해 관측 가능한 6가지 sub-case 를 구분한다. progress_store.json 에는 이
# 세분화된 값이 직접 기록되어 사용자가 통계 집계 가능.
REASON_URL_EXTRACT_HLS_ONLY = "url_extract_hls_only"           # m3u8/HLS 만 감지 — 다운로더 미지원
REASON_URL_EXTRACT_NO_PLAYER = "url_extract_no_player"         # iframe/player frame 미탐지
REASON_URL_EXTRACT_CONTENT_PHP_PARSE = "url_extract_content_php_parse"  # content.php 응답은 왔지만 XML 파싱/필드 부재
REASON_URL_EXTRACT_CONTENT_PHP_MISSING = "url_extract_content_php_missing"  # content.php 응답 자체가 안 옴
REASON_URL_EXTRACT_TIMEOUT = "url_extract_timeout"             # 60초 폴링 후에도 아무것도 관측 못함
REASON_URL_EXTRACT_EXCEPTION = "url_extract_exception"         # goto/navigation 등 예외 발생

# ── 재생/파이프라인 사유 (PlayResult 전용) ──────────────
REASON_PLAY_FAILED = "play_failed"                # 3회 재시도 후 재생 실패
REASON_STOPPED = "stopped"                        # 사용자 중단 신호

# ── 재시도 정책 중앙집중 ───────────────────────────────────
# 재시도해도 의미가 없는 "구조적 실패" 사유. auto.py 가 직접 튜플을 하드코드
# 하지 않고 이 함수를 호출하도록 통일해, 새 사유 추가 시 누락을 방지한다.
# SUSPICIOUS_STUB 은 DOM 폴링 타이밍 의존이라 extract_video_url 재실행으로
# 회복 가능 → retry 허용 대상.
_NO_RETRY_REASONS = frozenset({
    REASON_UNSUPPORTED,
    REASON_PATH_INVALID,
    REASON_SSRF_BLOCKED,
})


def is_no_retry_reason(reason: str | None) -> bool:
    """해당 사유로 실패한 다운로드를 재시도하지 않는 것이 옳은지 여부."""
    if not reason:
        return False
    return reason in _NO_RETRY_REASONS


@dataclass
class ExtractionResult:
    """extract_video_url 반환 타입.

    기존 `str | None` 을 대체해 sub-reason 과 진단 컨텍스트를 전달한다.
    호출자는 `.url` 로 기존처럼 사용 가능(Optional), 실패 시 `.reason` 으로
    세분화된 원인을, `.diagnostics` 로 Plan A/B 관찰 결과를 조사할 수 있다.
    """

    url: str | None = None
    reason: str | None = None  # 성공 시 None. 실패 시 REASON_URL_EXTRACT_* 중 하나.
    diagnostics: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.diagnostics is None:
            self.diagnostics = {}


@dataclass
class DownloadResult:
    """run_download 반환 타입.

    ok=True 이면 mp4는 다운로드 완료 상태. 부수 단계(mp3/stt/요약)는
    각자 성공 여부가 별도 필드에 담기지만 ok 판정에는 포함하지 않는다
    (Phase 1 관측성 범위에서는 mp4 성공만 "downloaded"로 간주).
    """

    ok: bool
    reason: str = ""
    mp4_path: Path | None = None
    mp3_path: Path | None = None
    txt_path: Path | None = None
    summary_path: Path | None = None


class SSRFBlockedError(ValueError):
    """허용 호스트/프로토콜 위반. _validate_media_url에서만 발생."""


class SuspiciousStubError(RuntimeError):
    """다운로드된 파일이 실제 강의가 아닌 가짜/스텁 파일로 의심됨.

    시그니처(예: WebM/EBML) 불일치 또는 비정상적으로 작은 크기 등을
    감지한 경우 발생. 플레이어의 fake webm 누출이나 CDN 인증 실패
    응답이 mp4 확장자로 저장되는 것을 차단한다.
    """
