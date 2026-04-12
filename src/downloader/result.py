"""다운로드 결과 데이터클래스와 실패 사유 상수.

run_download 및 관련 레이어가 성공/실패 상태를 구조화된 형태로 반환하기 위해 사용한다.
사유 상수는 study_helper.log와 auto_progress.json에 그대로 기록되므로
값을 바꿀 때는 로그 파서/복구 스크립트 호환성을 함께 점검할 것.
"""

from dataclasses import dataclass
from pathlib import Path

# ── 실패 사유 ─────────────────────────────────────────────
REASON_UNSUPPORTED = "unsupported"                # learningx 등 구조적 다운로드 불가
REASON_URL_EXTRACT_FAILED = "url_extract_failed"  # mp4 URL 추출 실패 (HLS 전용 플레이어 등)
REASON_SSRF_BLOCKED = "ssrf_blocked"              # 허용 호스트/프로토콜 위반
REASON_NETWORK = "network"                        # 타임아웃, 연결 오류, 청크 손상
REASON_PATH_INVALID = "path_invalid"              # base_dir 벗어난 경로
REASON_MP3_FAILED = "mp3_convert_failed"          # ffmpeg 변환 실패
REASON_UNKNOWN = "unknown"                        # 분류 불가


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
