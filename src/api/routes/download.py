"""다운로드/변환/STT/요약 엔드포인트."""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from src.config import Config
from src.logger import get_logger
from src.service.download_pipeline import (
    PipelineProgress,
    convert_to_audio,
    resolve_download_path,
    run_pipeline,
    summarize_text,
    transcribe_audio,
)

_log = get_logger("api.download")

_ALLOWED_WHISPER_MODELS = {"tiny", "base", "small", "medium", "large"}
_ALLOWED_AI_AGENTS = {"gemini", "openai"}

router = APIRouter()


class ConvertRequest(BaseModel):
    mp4_path: str
    delete_original: bool = False


class TranscribeRequest(BaseModel):
    audio_path: str
    model_size: str = "base"
    language: str = "ko"


class SummarizeRequest(BaseModel):
    txt_path: str
    agent: str = "gemini"
    api_key: str = ""
    model: str = ""
    extra_prompt: str = ""


class PipelineRequest(BaseModel):
    mp4_path: str
    course_name: str
    week_label: str = ""
    lecture_title: str = ""
    audio_only: bool = False
    both: bool = False


class ResolvePathRequest(BaseModel):
    course_name: str
    week_label: str = ""
    lecture_title: str = ""


@router.post("/resolve-path")
def resolve_path(body: ResolvePathRequest):
    """다운로드 경로를 결정한다."""
    download_dir = Config.get_download_dir()
    result = resolve_download_path(download_dir, body.course_name, body.week_label, body.lecture_title)
    if result is None:
        return {"error": "잘못된 경로"}
    return {"path": str(result)}


def _validate_path_in_download_dir(file_path: str) -> Path:
    """파일 경로가 다운로드 디렉토리 내에 있는지 검증한다."""
    from fastapi import HTTPException

    p = Path(file_path).resolve()
    base = Path(Config.get_download_dir()).resolve()
    if not p.is_relative_to(base):
        raise HTTPException(status_code=400, detail="허용되지 않은 파일 경로")
    return p


@router.post("/convert")
def convert(body: ConvertRequest):
    """mp4를 mp3로 변환한다."""
    mp4 = _validate_path_in_download_dir(body.mp4_path)
    mp3_path = convert_to_audio(mp4, delete_original=body.delete_original)
    return {"mp3_path": str(mp3_path)}


@router.post("/transcribe")
async def transcribe(body: TranscribeRequest):
    """음성을 텍스트로 변환한다 (STT)."""
    if body.model_size not in _ALLOWED_WHISPER_MODELS:
        raise HTTPException(status_code=400, detail=f"허용되지 않는 모델: {body.model_size}")
    audio = _validate_path_in_download_dir(body.audio_path)
    loop = asyncio.get_running_loop()
    try:
        txt_path = await loop.run_in_executor(
            None,
            lambda: transcribe_audio(audio, model_size=body.model_size, language=body.language),
        )
        return {"txt_path": str(txt_path)}
    finally:
        # STT 모델 메모리 해제 (수백 MB) — API 서버 장시간 운영 시 메모리 누적 방지
        try:
            from src.stt.transcriber import unload_model

            await loop.run_in_executor(None, unload_model)
        except Exception:
            pass


@router.post("/summarize")
async def summarize(body: SummarizeRequest):
    """텍스트를 AI로 요약한다."""
    if body.agent not in _ALLOWED_AI_AGENTS:
        raise HTTPException(status_code=400, detail=f"허용되지 않는 AI 에이전트: {body.agent}")
    txt = _validate_path_in_download_dir(body.txt_path)
    loop = asyncio.get_running_loop()
    summary_path = await loop.run_in_executor(
        None,
        lambda: summarize_text(
            txt,
            agent=body.agent,
            api_key=body.api_key,
            model=body.model,
            extra_prompt=body.extra_prompt,
        ),
    )
    return {"summary_path": str(summary_path)}


@router.websocket("/pipeline")
async def pipeline_ws(ws: WebSocket):
    """다운로드 후속 파이프라인을 WebSocket으로 실행한다.

    첫 번째 메시지로 토큰 인증 후 PipelineRequest를 수신하면,
    각 단계의 진행 상태를 JSON 메시지로 스트리밍한다.
    """
    await ws.accept()
    pipeline_task: asyncio.Task | None = None
    try:
        # WebSocket 토큰 인증 (첫 메시지) — 상수시간 비교.
        # SEC-003: 토큰 미설정 시에도 fail-closed. STUDY_HELPER_API_ALLOW_NO_TOKEN=1
        # 명시 플래그로만 우회 허용 (server.py 부팅 단계에서 이미 검증됨 — 여기 도달하면
        # 토큰이 있거나 명시적 우회 모드 둘 중 하나).
        _api_token = os.getenv("STUDY_HELPER_API_TOKEN", "")
        _allow_no_token = os.getenv("STUDY_HELPER_API_ALLOW_NO_TOKEN", "") == "1"
        if _api_token:
            auth_msg = await ws.receive_json()
            _client_token = auth_msg.get("token") or ""
            if not secrets.compare_digest(_client_token, _api_token):
                await ws.send_json({"type": "error", "message": "인증 실패"})
                await ws.close(code=4003)
                return
        elif not _allow_no_token:
            # 방어적 중복 검증 — server.py 의 부팅 가드가 이미 걸렀어야 함.
            await ws.close(code=4003)
            return

        data = await ws.receive_json()
        req = PipelineRequest(**data)
        _validate_path_in_download_dir(req.mp4_path)

        async def _on_progress(p: PipelineProgress):
            await ws.send_json(
                {
                    "type": "progress",
                    "stage": p.stage.value,
                    "progress": p.progress,
                    "current": p.current,
                    "total": p.total,
                    "message": p.message,
                }
            )

        # Config에서 설정 로드
        tg = Config.get_telegram_credentials()
        # 파이프라인을 별도 태스크로 실행 — 클라이언트 disconnect 시 cancel 하기 위함.
        pipeline_task = asyncio.create_task(
            run_pipeline(
                mp4_path=Path(req.mp4_path),
                course_name=req.course_name,
                week_label=req.week_label,
                lecture_title=req.lecture_title,
                audio_only=req.audio_only,
                both=req.both,
                stt_enabled=Config.STT_ENABLED == "true",
                stt_model=Config.WHISPER_MODEL or "base",
                stt_language=Config.STT_LANGUAGE,
                ai_enabled=Config.AI_ENABLED == "true",
                ai_agent=Config.AI_AGENT or "gemini",
                ai_api_key=Config.GOOGLE_API_KEY if Config.AI_AGENT == "gemini" else Config.OPENAI_API_KEY,
                ai_model=Config.GEMINI_MODEL,
                ai_extra_prompt=Config.SUMMARY_PROMPT_EXTRA,
                tg_token=tg[0] if tg else "",
                tg_chat_id=tg[1] if tg else "",
                tg_auto_delete=Config.TELEGRAM_AUTO_DELETE == "true",
                on_progress=_on_progress,
            )
        )
        result = await pipeline_task

        await ws.send_json(
            {
                "type": "complete",
                "success": result.success,
                "mp4_path": str(result.mp4_path) if result.mp4_path else None,
                "mp3_path": str(result.mp3_path) if result.mp3_path else None,
                "txt_path": str(result.txt_path) if result.txt_path else None,
                "summary_path": str(result.summary_path) if result.summary_path else None,
                "error": result.error,
                "stage_errors": result.stage_errors,
            }
        )
        await ws.close()
    except WebSocketDisconnect:
        # 클라이언트 disconnect — 실행 중인 파이프라인을 취소해 유휴 CPU/메모리 낭비 방지
        if pipeline_task and not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        _log.error("Pipeline WebSocket 오류: %s", e, exc_info=True)
        if pipeline_task and not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await ws.send_json({"type": "error", "message": "파이프라인 실행 중 오류가 발생했습니다"})
            await ws.close()
        except Exception:
            pass
