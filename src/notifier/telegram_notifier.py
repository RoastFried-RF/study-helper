"""
텔레그램 봇 알림 모듈.

재생 완료 알림과 AI 요약 결과 전송 기능을 제공한다.

`sendMessage`/`sendDocument` 호출 시 parse_mode 를 지정하지 않으므로
Telegram 은 본문을 plain text 로 해석한다 → 강의명에 `<`, `*`, `_` 등이
섞여도 마크업으로 재해석되지 않는다.
"""

import re
import time
from pathlib import Path

import requests

from src.config import RetryPolicy
from src.logger import get_logger

_log = get_logger("notifier.telegram")

# Telegram 메시지 최대 길이 (API 명세)
_TELEGRAM_MAX_MESSAGE_LEN = 4096
# Telegram Bot API sendDocument 파일 크기 한도 (약 50MB)
_TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
# 봇 토큰 형식: 숫자:영문숫자_하이픈 (URL 특수문자 방지)
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
# 일시 장애 재시도 정책 — 최대 3회, exponential backoff (1s, 2s).
# 5xx 또는 network 예외만 retry. 4xx(잘못된 chat_id 등) 는 즉시 실패 처리.
# ARCH-010: RetryPolicy 전역 정책 사용.
_MAX_RETRIES = RetryPolicy.TELEGRAM
_RETRY_BASE_DELAY = RetryPolicy.TELEGRAM_BASE_DELAY


def _is_retriable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _validate_token(bot_token: str) -> bool:
    """봇 토큰 형식 검증 (URL 특수문자 방지)."""
    return bool(_BOT_TOKEN_RE.match(bot_token))


def _request_with_retry(
    endpoint: str,
    bot_token: str,
    timeout: float,
    *,
    json: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
) -> bool:
    """Telegram Bot API 호출을 retry/backoff 공통 로직으로 감싼다.

    ARCH-005: _send_message 와 _send_document 가 동일한 retry 로직을 복제하던 것을 통합.
    5xx/429 는 재시도, 4xx 는 즉시 실패. 최대 3회 + exponential backoff(1s, 2s).
    """
    if not _validate_token(bot_token):
        return False
    url = f"https://api.telegram.org/bot{bot_token}/{endpoint}"
    last_error: str | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, json=json, data=data, files=files, timeout=timeout)
            try:
                if resp.ok:
                    try:
                        return resp.json().get("ok", False)
                    except ValueError:
                        return False
                if not _is_retriable_status(resp.status_code):
                    # 4xx — chat_id 오류 등. 재시도 무의미.
                    _log.warning("Telegram %s %d — 재시도 안 함", endpoint, resp.status_code)
                    return False
                last_error = f"status={resp.status_code}"
            finally:
                resp.close()
        except requests.exceptions.RequestException as e:
            last_error = type(e).__name__
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BASE_DELAY * (2**attempt))
    _log.warning("Telegram %s 최종 실패: %s", endpoint, last_error)
    return False


def _send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """텔레그램 메시지를 전송한다."""
    return _request_with_retry(
        "sendMessage",
        bot_token,
        timeout=10,
        json={"chat_id": chat_id, "text": text},
    )


def _send_message_verify(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """verify 전용 sendMessage — 실패 사유를 4xx/5xx/network 로 구분 반환한다.

    API-F4: 일반 `_send_message` 는 모든 실패를 `False` 로 뭉뚱그려
    `verify_bot` 이 5xx/네트워크 장애까지 `INVALID_CHAT_ID` 로 오분류했다.
    verify 경로에서는 status code 를 직접 검사해 사유를 분리한다.

    Returns:
        (성공 여부, 오류 코드) — 오류 코드는 빈 문자열(성공) /
        INVALID_CHAT_ID / NETWORK_ERROR / TELEGRAM_API_ERROR 중 하나.
    """
    if not _validate_token(bot_token):
        return False, "INVALID_TOKEN_FORMAT"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    last_status: int | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                url, json={"chat_id": chat_id, "text": text}, timeout=10
            )
            try:
                if resp.ok:
                    try:
                        if resp.json().get("ok", False):
                            return True, ""
                    except ValueError:
                        return False, "TELEGRAM_API_ERROR"
                    return False, "TELEGRAM_API_ERROR"
                last_status = resp.status_code
                if not _is_retriable_status(resp.status_code):
                    # 4xx — 잘못된 chat_id 등. 재시도 무의미.
                    _log.warning(
                        "Telegram sendMessage(verify) %d — 재시도 안 함",
                        resp.status_code,
                    )
                    return False, "INVALID_CHAT_ID"
            finally:
                resp.close()
        except requests.exceptions.RequestException as e:
            _log.warning(
                "Telegram sendMessage(verify) 네트워크 오류: %s", type(e).__name__
            )
            last_status = None
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BASE_DELAY * (2**attempt))
    # 재시도 소진 — 5xx/429 이면 API 오류, 네트워크 예외면 NETWORK_ERROR.
    if last_status is not None:
        _log.warning("Telegram sendMessage(verify) 최종 실패: status=%d", last_status)
        return False, "TELEGRAM_API_ERROR"
    return False, "NETWORK_ERROR"


def _send_document(bot_token: str, chat_id: str, file_path: Path, caption: str = "") -> bool:
    """텔레그램 파일을 전송한다. 50MB 초과 파일은 전송 시도 없이 False."""
    if not _validate_token(bot_token):
        return False
    try:
        size = file_path.stat().st_size
    except OSError:
        return False
    if size > _TELEGRAM_MAX_DOCUMENT_BYTES:
        _log.warning(
            "Telegram sendDocument: 파일이 50MB 초과 — 전송 생략 (%d bytes): %s",
            size, file_path.name,
        )
        return False
    # retry 루프에서 동일 bytes 를 재사용할 수 있도록 미리 로드 (50MB 상한)
    try:
        body = file_path.read_bytes()
    except OSError:
        return False
    return _request_with_retry(
        "sendDocument",
        bot_token,
        timeout=60,
        data={"chat_id": chat_id, "caption": caption},
        files={"document": (file_path.name, body)},
    )


def _lecture_label(course_name: str, week_label: str, lecture_title: str) -> str:
    """'과목-주차 강의명' 형식의 레이블을 반환한다."""
    parts = []
    if course_name:
        parts.append(course_name)
    if week_label:
        parts.append(week_label)
    prefix = "-".join(parts)
    if prefix:
        return f"{prefix} {lecture_title}"
    return lecture_title


def notify_playback_complete(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> bool:
    """영상 재생 완료 알림을 전송한다."""
    label = _lecture_label(course_name, week_label, lecture_title)
    text = f"[알림] {label} 시청을 완료하였습니다."
    return _send_message(bot_token, chat_id, text)


def notify_playback_error(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
    failed: bool = True,
) -> bool:
    """영상 재생 실패 또는 미완료 알림을 전송한다.

    Args:
        failed: True면 '재생을 실패', False면 '재생을 완료하지 못함'
    """
    label = _lecture_label(course_name, week_label, lecture_title)
    if failed:
        text = f"[오류] {label} 재생을 실패하였습니다."
    else:
        text = f"[오류] {label} 재생을 완료하지 못하였습니다."
    return _send_message(bot_token, chat_id, text)


def notify_download_error(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> bool:
    """다운로드 실패 알림을 전송한다."""
    label = _lecture_label(course_name, week_label, lecture_title)
    text = f"[오류] {label} 다운로드에 실패하였습니다."
    return _send_message(bot_token, chat_id, text)


def notify_download_unsupported(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> bool:
    """다운로드 불가 강의 알림을 전송한다."""
    label = _lecture_label(course_name, week_label, lecture_title)
    text = f"[안내] {label} 은(는) 다운로드가 지원되지 않는 강의입니다."
    return _send_message(bot_token, chat_id, text)


def notify_auto_error(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
    error_msg: str,
) -> bool:
    """자동 모드 처리 오류 알림을 전송한다."""
    label = _lecture_label(course_name, week_label, lecture_title)
    text = f"[자동 모드 오류] {label}\n{error_msg}"
    return _send_message(bot_token, chat_id, text)


def notify_download_gaps(
    bot_token: str,
    chat_id: str,
    missing: list[tuple[str, str, str, str]],
) -> bool:
    """다운로드 누락 점검 결과를 전송한다.

    Args:
        missing: [(course_name, week_label, title, file_type), ...] 형태의 누락 목록
    """
    lines = [f"[다운로드 누락 점검] {len(missing)}건 감지"]
    for course_name, week, title, ftype in missing[:10]:
        lines.append(f"  • {course_name} {week} {title} ({ftype})")
    if len(missing) > 10:
        lines.append(f"  ... 외 {len(missing) - 10}건")
    return _send_message(bot_token, chat_id, "\n".join(lines))


def notify_summary_complete(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
    summary_text: str,
    summary_path: Path,
    auto_delete_files: list[Path] | None = None,
) -> bool:
    """AI 요약 완료 알림을 전송한다. 요약 내용을 메시지로, 파일도 함께 첨부한다.
    전송 성공 시 auto_delete_files에 포함된 파일을 삭제한다.
    """
    label = _lecture_label(course_name, week_label, lecture_title)
    header = f"[알림] {label}의 요약 내용을 다음과 같이 제공해드립니다.\n\n"
    text = header + summary_text

    # LOG-011: 요약 내용 텍스트를 4096자 청크로 분할 전송.
    # 중간 청크 실패 시 남은 청크를 계속 보내면 사용자에게 잘린 요약이 도착한다.
    # 첫 실패에서 즉시 break 하고 첨부 파일(요약 전문) 전송으로 복구한다.
    # 여러 청크인 경우 사용자가 손실을 인지할 수 있도록 순서 마커 "(n/N)" 부여.
    chunks = [text[i : i + _TELEGRAM_MAX_MESSAGE_LEN] for i in range(0, len(text), _TELEGRAM_MAX_MESSAGE_LEN)]
    total_chunks = len(chunks)
    msg_ok = True
    for idx, chunk in enumerate(chunks, start=1):
        marked = f"({idx}/{total_chunks}) {chunk}" if total_chunks > 1 else chunk
        if not _send_message(bot_token, chat_id, marked):
            msg_ok = False
            break  # 나머지 청크 전송 중단 — 잘린 요약 대신 첨부 파일로 복구 유도

    # 요약 파일 첨부 전송 — 청크 일부 실패해도 전문이 첨부되면 사용자 복구 가능
    file_ok = _send_document(bot_token, chat_id, summary_path, caption=f"{label} 요약 파일")

    success = msg_ok and file_ok

    if success and auto_delete_files:
        for p in auto_delete_files:
            try:
                if p and p.is_file():
                    p.unlink()
            except Exception:
                pass

    return success


def notify_deadline_warning(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
    type_label: str,
    end_date: str,
    remaining_hours: float,
) -> bool:
    """마감 임박 알림을 전송한다."""
    if remaining_hours >= 1:
        time_text = f"약 {int(remaining_hours)}시간 남음"
    else:
        time_text = f"약 {int(remaining_hours * 60)}분 남음"
    label = _lecture_label(course_name, week_label, lecture_title)
    text = f"[마감 임박] {label}\n{type_label} | 마감: {end_date} ({time_text})"
    return _send_message(bot_token, chat_id, text)


def notify_summary_send_error(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> bool:
    """요약 내용 발송 실패 알림을 전송한다."""
    label = _lecture_label(course_name, week_label, lecture_title)
    text = f"[오류] {label} 요약 내용 발송에 실패하였습니다."
    return _send_message(bot_token, chat_id, text)


def verify_bot(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """봇 토큰과 chat ID가 유효한지 확인하고 테스트 메시지를 전송한다.

    봇 토큰 형식 검증 → getMe API 호출 → 테스트 메시지 전송 순서로 진행한다.

    Returns:
        (성공 여부, 오류 메시지 또는 빈 문자열)
    """
    if not _validate_token(bot_token):
        return False, "INVALID_TOKEN_FORMAT"

    # API-F5: 빈 chat_id 는 텔레그램 API 왕복 없이 즉시 거부한다.
    if not chat_id:
        return False, "INVALID_CHAT_ID"

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=10,
        )
        try:
            if not resp.ok:
                # SEC-009: Telegram 응답 원문에 토큰 일부/요청 URL 이 포함될 수 있어
                # 응답으로 노출하지 않는다. 상세는 서버 로그에만.
                try:
                    desc = resp.json().get("description", "")
                except ValueError:
                    desc = resp.text[:200]
                _log.warning("Telegram getMe 실패 (%d): %s", resp.status_code, desc)
                if resp.status_code in (401, 404):
                    return False, "INVALID_TOKEN"
                return False, "TELEGRAM_API_ERROR"
            try:
                bot_name = resp.json().get("result", {}).get("username", "")
            except ValueError:
                return False, "TELEGRAM_API_ERROR"
        finally:
            resp.close()
    except Exception as e:
        _log.warning("Telegram getMe 네트워크 오류: %s: %s", type(e).__name__, e)
        return False, "NETWORK_ERROR"

    # API-F4: 실패 사유를 4xx(INVALID_CHAT_ID)/5xx(TELEGRAM_API_ERROR)/network
    # (NETWORK_ERROR)로 구분 반환 — 무조건 INVALID_CHAT_ID 오분류 방지.
    ok, err_code = _send_message_verify(
        bot_token, chat_id, f"[알림] study-helper 텔레그램 알림이 연결되었습니다! (봇: @{bot_name})"
    )
    if not ok:
        return False, err_code or "INVALID_CHAT_ID"

    return True, ""
