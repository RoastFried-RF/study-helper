"""
다운로드 파이프라인 서비스.

다운로드 → 변환 → STT → AI 요약 → 텔레그램 알림을 UI 독립적으로 처리한다.
각 단계의 진행 상태는 콜백으로 전달된다.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_log = logging.getLogger(__name__)


class PipelineStage(Enum):
    """파이프라인 단계."""

    DOWNLOAD = "download"
    CONVERT = "convert"
    TRANSCRIBE = "transcribe"
    SUMMARIZE = "summarize"
    NOTIFY = "notify"


@dataclass
class PipelineProgress:
    """파이프라인 진행 상태."""

    stage: PipelineStage
    progress: float = 0.0  # 0.0 ~ 1.0
    current: int = 0
    total: int = 0
    message: str = ""


@dataclass
class PipelineResult:
    """파이프라인 실행 결과."""

    success: bool
    mp4_path: Path | None = None
    mp3_path: Path | None = None
    txt_path: Path | None = None
    summary_path: Path | None = None
    error: str = ""
    stage_errors: dict[str, str] = field(default_factory=dict)


# 콜백 타입: 동기/비동기 모두 지원 (_emit에서 isawaitable 체크)
ProgressCallback = Callable[[PipelineProgress], None]  # sync or async


def resolve_download_path(
    download_dir: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> Path | None:
    """다운로드 경로를 결정하고 경계 검증을 수행한다.

    Returns:
        mp4 파일의 절대 경로. 경로 검증 실패 시 None.
    """
    from src.downloader.video_downloader import make_filepath

    mp4_relpath = make_filepath(course_name, week_label, lecture_title)
    mp4_path = (Path(download_dir) / mp4_relpath).resolve()
    base_dir = Path(download_dir).resolve()

    if not mp4_path.is_relative_to(base_dir):
        return None
    return mp4_path


def convert_to_audio(mp4_path: Path, delete_original: bool = False) -> Path:
    """mp4를 mp3로 변환한다.

    Args:
        mp4_path: 원본 mp4 경로
        delete_original: True면 변환 후 mp4 삭제

    Returns:
        mp3 파일 경로
    """
    from src.converter.audio_converter import convert_to_mp3

    mp3_path = convert_to_mp3(mp4_path)
    if delete_original:
        mp4_path.unlink(missing_ok=True)
    return mp3_path


def transcribe_audio(
    audio_path: Path,
    model_size: str = "base",
    language: str = "ko",
) -> Path:
    """음성 파일을 텍스트로 변환한다.

    Returns:
        텍스트 파일 경로
    """
    from src.stt.transcriber import transcribe

    return transcribe(audio_path, model_size=model_size, language=language)


def summarize_text(
    txt_path: Path,
    agent: str = "gemini",
    api_key: str = "",
    model: str = "",
    extra_prompt: str = "",
) -> Path:
    """텍스트를 AI로 요약한다.

    Returns:
        요약 파일 경로
    """
    from src.summarizer.summarizer import GEMINI_DEFAULT_MODEL, summarize

    return summarize(
        txt_path,
        agent=agent,
        api_key=api_key,
        model=model or GEMINI_DEFAULT_MODEL,
        extra_prompt=extra_prompt,
    )


def send_summary_notification(
    token: str,
    chat_id: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
    summary_path: Path,
    auto_delete_files: list[Path] | None = None,
) -> bool:
    """텔레그램으로 요약 결과를 전송한다."""
    from src.notifier.telegram_notifier import notify_summary_complete

    summary_text = summary_path.read_text(encoding="utf-8").strip()
    return notify_summary_complete(
        bot_token=token,
        chat_id=chat_id,
        course_name=course_name,
        week_label=week_label,
        lecture_title=lecture_title,
        summary_text=summary_text,
        summary_path=summary_path,
        auto_delete_files=auto_delete_files,
    )


async def run_pipeline(
    mp4_path: Path,
    course_name: str,
    week_label: str,
    lecture_title: str,
    audio_only: bool = False,
    both: bool = False,
    stt_enabled: bool = False,
    stt_model: str = "base",
    stt_language: str = "ko",
    ai_enabled: bool = False,
    ai_agent: str = "gemini",
    ai_api_key: str = "",
    ai_model: str = "",
    ai_extra_prompt: str = "",
    tg_token: str = "",
    tg_chat_id: str = "",
    tg_auto_delete: bool = False,
    on_progress: ProgressCallback | None = None,
) -> PipelineResult:
    """다운로드 후속 파이프라인 (변환 → STT → 요약 → 알림)을 실행한다.

    mp4 파일이 이미 다운로드되어 있다고 가정한다.
    다운로드 자체는 Playwright/Electron 의존이므로 이 함수에 포함하지 않는다.

    Args:
        mp4_path:       다운로드 완료된 mp4 경로
        course_name:    과목명
        week_label:     주차 레이블
        lecture_title:  강의 제목
        audio_only:     mp3만 유지 (mp4 삭제)
        both:           mp4 + mp3 둘 다 유지
        stt_*:          STT 설정
        ai_*:           AI 요약 설정
        tg_*:           텔레그램 설정
        on_progress:    진행 콜백

    Returns:
        PipelineResult
    """
    result = PipelineResult(success=True, mp4_path=mp4_path)

    async def _emit(stage: PipelineStage, progress: float = 0.0, msg: str = ""):
        if on_progress:
            ret = on_progress(PipelineProgress(stage=stage, progress=progress, message=msg))
            if inspect.isawaitable(ret):
                await ret

    # SEC-005: stage_errors 에는 고정 에러 코드(타입명)만 노출한다.
    # 원본 예외 메시지는 서버 로그에만 기록해 API 응답 leak 을 방지한다.
    # traceback 은 exc_info=False 로 frame locals (API 키/토큰) 유출을 차단.

    # ── 1. mp3 변환 ──────────────────────────────────────────────
    if audio_only or both:
        await _emit(PipelineStage.CONVERT, 0.0, "mp3 변환 중...")
        try:
            result.mp3_path = convert_to_audio(mp4_path, delete_original=audio_only)
            if audio_only:
                result.mp4_path = None
            await _emit(PipelineStage.CONVERT, 1.0, "mp3 변환 완료")
        except Exception as e:
            _log.error("convert 단계 실패: %s: %s", type(e).__name__, e, exc_info=False)
            result.stage_errors["convert"] = type(e).__name__
            result.success = False
            result.error = "CONVERT_FAILED"
            return result

    # ── 2. STT ───────────────────────────────────────────────────
    if result.mp3_path and stt_enabled:
        await _emit(PipelineStage.TRANSCRIBE, 0.0, "STT 변환 중...")
        try:
            loop = asyncio.get_running_loop()
            result.txt_path = await loop.run_in_executor(
                None,
                lambda: transcribe_audio(result.mp3_path, model_size=stt_model, language=stt_language),
            )
            await _emit(PipelineStage.TRANSCRIBE, 1.0, "STT 완료")
        except Exception as e:
            _log.error("transcribe 단계 실패: %s: %s", type(e).__name__, e, exc_info=False)
            result.stage_errors["transcribe"] = type(e).__name__
        finally:
            # STT 모델 메모리 해제 (수백 MB) — 파이프라인 완료 후 불필요
            try:
                from src.stt.transcriber import unload_model

                await loop.run_in_executor(None, unload_model)
            except Exception:
                pass

    # ── 3. AI 요약 ───────────────────────────────────────────────
    if result.txt_path and ai_enabled and ai_api_key:
        # B4: STT 결과가 비어 있으면 요약 호출을 생략 (API 비용/실패 알림 방지)
        from src.stt.transcriber import is_transcript_usable

        if not is_transcript_usable(result.txt_path):
            result.stage_errors["summarize"] = "TRANSCRIPT_EMPTY"
            await _emit(PipelineStage.SUMMARIZE, 1.0, "STT 결과 없음 — 요약 생략")
        else:
            await _emit(PipelineStage.SUMMARIZE, 0.0, "AI 요약 중...")
            try:
                loop = asyncio.get_running_loop()
                result.summary_path = await loop.run_in_executor(
                    None,
                    lambda: summarize_text(
                        result.txt_path,
                        agent=ai_agent,
                        api_key=ai_api_key,
                        model=ai_model,
                        extra_prompt=ai_extra_prompt,
                    ),
                )
                await _emit(PipelineStage.SUMMARIZE, 1.0, "AI 요약 완료")
            except Exception as e:
                _log.error("summarize 단계 실패: %s: %s", type(e).__name__, e, exc_info=False)
                result.stage_errors["summarize"] = type(e).__name__

    # ── 4. 텔레그램 알림 ─────────────────────────────────────────
    if result.summary_path and tg_token and tg_chat_id:
        await _emit(PipelineStage.NOTIFY, 0.0, "텔레그램 전송 중...")
        files_to_delete = None
        if tg_auto_delete:
            files_to_delete = [f for f in [result.mp4_path, result.mp3_path, result.txt_path, result.summary_path] if f]
        try:
            ok = send_summary_notification(
                token=tg_token,
                chat_id=tg_chat_id,
                course_name=course_name,
                week_label=week_label,
                lecture_title=lecture_title,
                summary_path=result.summary_path,
                auto_delete_files=files_to_delete,
            )
            await _emit(PipelineStage.NOTIFY, 1.0, "전송 완료" if ok else "전송 실패")
            if not ok:
                result.stage_errors["notify"] = "NOTIFY_FAILED"
        except Exception as e:
            _log.error("notify 단계 실패: %s: %s", type(e).__name__, e, exc_info=False)
            result.stage_errors["notify"] = type(e).__name__

    return result
