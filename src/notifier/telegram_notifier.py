"""
텔레그램 봇 알림 모듈.

재생 완료 알림과 AI 요약 결과 전송 기능을 제공한다.
"""

from pathlib import Path
from typing import Optional


def _send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """텔레그램 메시지를 전송한다. 성공 시 True 반환."""
    import requests

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.ok
    except Exception:
        return False


def _send_document(bot_token: str, chat_id: str, file_path: Path, caption: str = "") -> bool:
    """텔레그램 파일을 전송한다. 성공 시 True 반환."""
    import requests

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (file_path.name, f)},
                timeout=60,
            )
        return resp.ok
    except Exception:
        return False


def notify_playback_complete(
    bot_token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> bool:
    """
    영상 재생 완료 알림을 전송한다.

    Args:
        bot_token:     텔레그램 봇 토큰
        chat_id:       수신자 chat ID
        course_name:   과목명
        week_label:    주차 레이블 (예: "1주차")
        lecture_title: 강의 제목

    Returns:
        전송 성공 여부
    """
    lines = ["✅ <b>강의 시청 완료</b>", ""]
    if course_name:
        lines.append(f"📚 과목: {course_name}")
    if week_label:
        lines.append(f"📅 주차: {week_label}")
    lines.append(f"🎬 강의: {lecture_title}")
    lines.append("")
    lines.append("LMS에 출석이 자동 처리되었습니다.")

    return _send_message(bot_token, chat_id, "\n".join(lines))


def notify_summary_complete(
    bot_token: str,
    chat_id: str,
    course_name: str,
    lecture_title: str,
    summary_path: Path,
    auto_delete_files: Optional[list[Path]] = None,
) -> bool:
    """
    AI 요약 완료 알림 및 요약 파일을 전송한다.
    전송 성공 시 auto_delete_files에 포함된 파일을 삭제한다.

    Args:
        bot_token:          텔레그램 봇 토큰
        chat_id:            수신자 chat ID
        course_name:        과목명
        lecture_title:      강의 제목
        summary_path:       요약 결과 .txt 파일 경로
        auto_delete_files:  전송 성공 후 삭제할 파일 목록 (None이면 삭제 안 함)

    Returns:
        전송 성공 여부
    """
    caption_lines = ["📝 <b>AI 요약 완료</b>", ""]
    if course_name:
        caption_lines.append(f"📚 과목: {course_name}")
    caption_lines.append(f"🎬 강의: {lecture_title}")

    success = _send_document(
        bot_token, chat_id, summary_path, caption="\n".join(caption_lines)
    )

    if success and auto_delete_files:
        for path in auto_delete_files:
            try:
                if path and Path(path).exists():
                    Path(path).unlink()
            except Exception:
                pass

    return success


def verify_bot(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """
    봇 토큰과 chat ID가 유효한지 확인하고 테스트 메시지를 전송한다.

    Returns:
        (성공 여부, 오류 메시지 또는 빈 문자열)
    """
    import requests

    # 봇 정보 확인
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=10,
        )
        if not resp.ok:
            data = resp.json()
            desc = data.get("description", resp.text)
            return False, f"봇 토큰 오류: {desc}"
        bot_name = resp.json().get("result", {}).get("username", "")
    except Exception as e:
        return False, f"네트워크 오류: {e}"

    # 테스트 메시지 전송
    ok = _send_message(
        bot_token, chat_id,
        f"✅ study-helper 텔레그램 알림이 연결되었습니다!\n봇: @{bot_name}"
    )
    if not ok:
        return False, "메시지 전송 실패. Chat ID를 확인하세요."

    return True, ""
