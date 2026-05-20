"""알림 관련 엔드포인트."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from src.config import Config

router = APIRouter()


class DeadlineCheckRequest(BaseModel):
    """마감 체크에 필요한 과목/강의 데이터는 Electron 측에서 제공한다."""

    courses: list[dict]  # [{id, long_name, ...}, ...]
    details: list[dict | None]  # [{weeks: [...], ...}, ...]


@router.post("/deadline-check")
def deadline_check() -> dict[str, object]:
    """마감 임박 항목을 체크하고 텔레그램으로 알림을 전송한다.

    TODO(electron-integration): Electron 측이 LMS 스크래핑 결과(courses/details)를
    POST body 로 넘기도록 DeadlineCheckRequest 를 채워, 내부에서
    `check_and_notify_deadlines` 를 호출하는 구조로 교체. 현재는 스텁 상태.
    """
    tg = Config.get_telegram_credentials()
    if not tg:
        return {"sent": 0, "message": "텔레그램 미설정"}
    return {"sent": 0, "message": "Electron 측 데이터 연동 필요"}


class NotifyRequest(BaseModel):
    course_name: str
    week_label: str = ""
    lecture_title: str = ""
    message_type: str  # "playback_complete" | "playback_error" | "download_error"
    failed: bool = True


@router.post("/telegram")
async def send_notification(body: NotifyRequest) -> dict[str, object]:
    """텔레그램 알림을 전송한다.

    L8: notify_* 는 blocking telegram HTTP — to_thread 로 위임.
    """
    tg = Config.get_telegram_credentials()
    if not tg:
        return {"ok": False, "error": "텔레그램 미설정"}

    token, chat_id = tg

    if body.message_type == "playback_complete":
        from src.notifier.telegram_notifier import notify_playback_complete

        ok = await asyncio.to_thread(
            notify_playback_complete, token, chat_id, body.course_name, body.week_label, body.lecture_title
        )
    elif body.message_type == "playback_error":
        from src.notifier.telegram_notifier import notify_playback_error

        ok = await asyncio.to_thread(
            notify_playback_error, token, chat_id, body.course_name, body.week_label,
            body.lecture_title, body.failed,
        )
    elif body.message_type == "download_error":
        from src.notifier.telegram_notifier import notify_download_error

        ok = await asyncio.to_thread(
            notify_download_error, token, chat_id, body.course_name, body.week_label, body.lecture_title
        )
    else:
        return {"ok": False, "error": f"알 수 없는 메시지 타입: {body.message_type}"}

    return {"ok": ok}
