"""
다운로드 파이프라인 서비스.

다운로드 → 변환 → STT → AI 요약 → 텔레그램 알림을 UI 독립적으로 처리한다.
각 단계의 진행 상태는 콜백으로 전달된다.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from src.logger import get_logger

_log = get_logger("service.download_pipeline")


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


# 콜백 타입: 동기(None 반환) 와 비동기(Awaitable 반환) 둘 다 수용.
# _emit 내부에서 inspect.isawaitable 로 분기하여 await 여부를 결정한다.
# ARCH-001: 이 시그니처가 TUI(Rich console) 와 API(WebSocket) 양쪽의
# 단일 진입점. TUI 는 sync 콜백으로, WebSocket 은 async 콜백으로 사용.
ProgressCallback = Callable[[PipelineProgress], None | Awaitable[None]]


def resolve_download_path(
    download_dir: str,
    course_name: str,
    week_label: str,
    lecture_title: str,
) -> Path | None:
    """다운로드 경로를 결정하고 경계 검증을 수행한다.

    ARCH-002 리팩토링 후에도 유지: 순수 경로 계산 + base 검증은 API 라우트·
    서비스 레이어 양쪽에서 공통으로 필요하며 1-3줄 래핑이 아니라 검증 책임이
    있는 도우미 함수이기 때문이다 (TRAVERSAL 방어가 목적).
    """
    from src.downloader.video_downloader import make_filepath

    mp4_relpath = make_filepath(course_name, week_label, lecture_title)
    mp4_path = (Path(download_dir) / mp4_relpath).resolve()
    base_dir = Path(download_dir).resolve()

    if not mp4_path.is_relative_to(base_dir):
        return None
    return mp4_path


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

    ARCH-001: TUI(`ui/download.py::run_download`) 와 API(`/download/pipeline` WS)
    양쪽의 단일 엔진. 호출자는 on_progress 콜백으로 단계별 진행 상태를 받고
    자신의 표현 매체(Rich Panel / WebSocket JSON)로 렌더한다.

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
            from src.converter.audio_converter import convert_to_mp3

            result.mp3_path = convert_to_mp3(mp4_path)
            if audio_only:
                mp4_path.unlink(missing_ok=True)
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
        # LOG-002: loop 를 try 블록 바깥에서 선언. try 최상단에서 예외가 발생하면
        # finally 의 loop.run_in_executor 에서 UnboundLocalError 가 원본 예외를
        # 덮어쓴다. 여기서 선언하면 finally 진입 시 항상 유효한 값.
        loop = asyncio.get_running_loop()
        try:
            from src.stt.transcriber import transcribe

            result.txt_path = await loop.run_in_executor(
                None,
                lambda: transcribe(result.mp3_path, model_size=stt_model, language=stt_language),
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
                from src.summarizer.summarizer import GEMINI_DEFAULT_MODEL, summarize

                loop = asyncio.get_running_loop()
                _summary_model = ai_model or GEMINI_DEFAULT_MODEL
                result.summary_path = await loop.run_in_executor(
                    None,
                    lambda: summarize(
                        result.txt_path,
                        agent=ai_agent,
                        api_key=ai_api_key,
                        model=_summary_model,
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
            from src.notifier.telegram_notifier import notify_summary_complete

            summary_text = result.summary_path.read_text(encoding="utf-8").strip()
            ok = notify_summary_complete(
                bot_token=tg_token,
                chat_id=tg_chat_id,
                course_name=course_name,
                week_label=week_label,
                lecture_title=lecture_title,
                summary_text=summary_text,
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
