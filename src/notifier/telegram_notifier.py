"""
텔레그램 봇 알림 모듈.

재생 완료 알림과 AI 요약 결과 전송 기능을 제공한다.
"""

import re
from pathlib import Path

import requests

# Telegram 메시지 최대 길이 (API 명세)
_TELEGRAM_MAX_MESSAGE_LEN = 4096
# 봇 토큰 형식: 숫자:영문숫자_하이픈 (URL 특수문자 방지)
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


def _send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """텔레그램 메시지를 전송한다. 응답 body의 ok 필드로 성공 여부를 판정한다."""
    if not _BOT_TOKEN_RE.match(bot_token):
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        try:
            if resp.ok:
                try:
                    data = resp.json()
                except ValueError:
                    return False
                return data.get("ok", False)
            return False
        finally:
            resp.close()
    except Exception:
        return False


def _send_document(bot_token: str, chat_id: str, file_path: Path, caption: str = "") -> bool:
    """텔레그램 파일을 전송한다. 응답 body의 ok 필드로 성공 여부를 판정한다."""
    if not _BOT_TOKEN_RE.match(bot_token):
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (file_path.name, f)},
                timeout=60,
            )
        try:
            if resp.ok:
                try:
                    data = resp.json()
                except ValueError:
                    return False
                return data.get("ok", False)
            return False
        finally:
            resp.close()
    except Exception:
        return False


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

    # 요약 내용 텍스트 메시지 전송 (4096자 초과 시 순차 전송, 실패해도 나머지 계속)
    msg_ok = True
    for i in range(0, len(text), _TELEGRAM_MAX_MESSAGE_LEN):
        if not _send_message(bot_token, chat_id, text[i : i + _TELEGRAM_MAX_MESSAGE_LEN]):
            msg_ok = False

    # 요약 파일 첨부 전송
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

    Returns:
        (성공 여부, 오류 메시지 또는 빈 문자열)
    """

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=10,
        )
        try:
            if not resp.ok:
                try:
                    data = resp.json()
                    desc = data.get("description", resp.text)
                except ValueError:
                    desc = resp.text
                return False, f"봇 토큰 오류: {desc}"
            try:
                bot_name = resp.json().get("result", {}).get("username", "")
            except ValueError:
                return False, "봇 응답 파싱 실패"
        finally:
            resp.close()
    except Exception as e:
        return False, f"네트워크 오류: {e}"

    ok = _send_message(bot_token, chat_id, f"[알림] study-helper 텔레그램 알림이 연결되었습니다! (봇: @{bot_name})")
    if not ok:
        return False, "메시지 전송 실패. Chat ID를 확인하세요."

    return True, ""
