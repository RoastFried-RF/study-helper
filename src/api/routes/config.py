"""설정 조회/저장 엔드포인트."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from src.config import Config

router = APIRouter()


class SettingsResponse(BaseModel):
    download_dir: str
    download_rule: str
    stt_enabled: str
    stt_language: str
    whisper_model: str
    ai_enabled: str
    ai_agent: str
    gemini_model: str
    summary_prompt_extra: str
    telegram_enabled: str
    telegram_chat_id: str
    telegram_auto_delete: str


class SettingsUpdate(BaseModel):
    download_dir: str = ""
    download_rule: str = ""
    stt_enabled: bool = False
    ai_enabled: bool = False
    ai_agent: str = ""
    api_key: str = ""
    gemini_model: str = ""
    summary_prompt_extra: str = ""


class TelegramUpdate(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    auto_delete: bool = False


@router.get("", response_model=SettingsResponse)
def get_settings() -> SettingsResponse:
    """현재 설정을 조회한다."""
    return SettingsResponse(
        download_dir=Config.get_download_dir(),
        download_rule=Config.DOWNLOAD_RULE,
        stt_enabled=Config.STT_ENABLED,
        stt_language=Config.STT_LANGUAGE,
        whisper_model=Config.WHISPER_MODEL,
        ai_enabled=Config.AI_ENABLED,
        ai_agent=Config.AI_AGENT,
        gemini_model=Config.GEMINI_MODEL,
        summary_prompt_extra=Config.SUMMARY_PROMPT_EXTRA,
        telegram_enabled=Config.TELEGRAM_ENABLED,
        telegram_chat_id=Config.TELEGRAM_CHAT_ID,
        telegram_auto_delete=Config.TELEGRAM_AUTO_DELETE,
    )


@router.put("")
def update_settings(body: SettingsUpdate) -> dict[str, str]:
    """설정을 저장한다."""
    Config.save_settings(
        download_dir=body.download_dir,
        download_rule=body.download_rule,
        stt_enabled=body.stt_enabled,
        ai_enabled=body.ai_enabled,
        ai_agent=body.ai_agent,
        api_key=body.api_key,
        gemini_model=body.gemini_model,
        summary_prompt_extra=body.summary_prompt_extra,
    )
    return {"status": "ok"}


@router.put("/telegram")
def update_telegram(body: TelegramUpdate) -> dict[str, str]:
    """텔레그램 설정을 저장한다."""
    Config.save_telegram(
        enabled=body.enabled,
        bot_token=body.bot_token,
        chat_id=body.chat_id,
        auto_delete=body.auto_delete,
    )
    return {"status": "ok"}


@router.post("/telegram/verify")
async def verify_telegram(body: TelegramUpdate) -> dict[str, object]:
    """텔레그램 봇 연결을 테스트한다.

    L8: verify_bot 는 blocking telegram HTTP(getMe + sendMessage) — to_thread 로
    위임해 FastAPI threadpool 슬롯 장기 점유를 줄인다.
    """
    from src.notifier.telegram_notifier import verify_bot

    ok, error = await asyncio.to_thread(verify_bot, body.bot_token, body.chat_id)
    return {"ok": ok, "error": error}


@router.get("/credentials")
def has_credentials() -> dict[str, bool]:
    """저장된 자격증명 존재 여부를 반환한다."""
    return {"has_credentials": Config.has_credentials()}
