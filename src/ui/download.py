"""
다운로드 관련 UI.

다운로드 진행률 화면을 제공한다.
다운로드 경로는 설정(settings)에서 관리하며, Config에서 직접 읽는다.
"""

import asyncio
from pathlib import Path

import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from src.config import Config
from src.downloader.result import (
    REASON_MP3_FAILED,
    REASON_NETWORK,
    REASON_PATH_INVALID,
    REASON_SSRF_BLOCKED,
    REASON_SUSPICIOUS_STUB,
    REASON_UNKNOWN,
    REASON_UNSUPPORTED,
    REASON_URL_EXTRACT_FAILED,
    DownloadResult,
    SSRFBlockedError,
    SuspiciousStubError,
)
from src.logger import get_error_logger
from src.utils import safe_url

_MAX_URL_RETRIES = 3
_RETRY_WAIT = 10  # seconds

console = Console()


async def run_download(page, lec, course, audio_only: bool = False, both: bool = False) -> DownloadResult:
    """
    강의 영상을 다운로드하고 진행률을 Progress bar로 표시한다.

    Args:
        page:       CourseScraper._page (Playwright Page)
        lec:        다운로드할 LectureItem
        course:     과목 Course (파일명 생성에 사용)
        audio_only: True면 mp3로 변환 후 mp4 삭제
        both:       True면 mp4 유지 + mp3도 추가 생성

    Returns:
        DownloadResult: ok=True면 mp4 다운로드까지 완료. 실패 시 reason에 분류된 사유가 담김.
    """
    from src.downloader.video_downloader import (
        download_video_with_browser,
        extract_video_url_detailed,
        make_filepath,
    )

    console.print()
    console.print(
        Panel(
            Text(lec.title, justify="center", style="bold cyan"),
            border_style="cyan",
            padding=(0, 4),
        )
    )
    console.print()

    download_dir = Config.get_download_dir()

    from src.notifier.telegram_dispatch import dispatch_if_configured

    # 1. 구조적으로 다운로드 불가능한 항목 조기 감지 (learningx 등)
    if not lec.is_downloadable:
        console.print("  [yellow]다운로드 불가:[/yellow] 이 강의는 다운로드가 지원되지 않는 형식입니다.")
        from src.notifier.telegram_notifier import notify_download_unsupported

        dispatch_if_configured(
            notify_download_unsupported,
            course_name=course.long_name,
            week_label=lec.week_label,
            lecture_title=lec.title,
        )
        return DownloadResult(ok=False, reason=REASON_UNSUPPORTED)

    # 2. video URL 추출 (최대 3회 재시도)
    # 구조적 실패(learningx LTI 전용, SSRF, path 위반 등)는 재시도해도 무의미하므로
    # is_no_retry_reason 으로 즉시 탈출해 총 30초 대기 루프를 건너뛴다.
    from src.downloader.result import is_no_retry_reason

    video_url = None
    last_extraction = None  # 마지막 시도의 ExtractionResult
    for attempt in range(1, _MAX_URL_RETRIES + 1):
        if attempt == 1:
            console.print("  [dim]영상 URL 추출 중...[/dim]")
        else:
            console.print(f"  [dim]영상 URL 추출 재시도 ({attempt}/{_MAX_URL_RETRIES})...[/dim]")
        last_extraction = await extract_video_url_detailed(page, lec.full_url)
        if last_extraction.url:
            video_url = last_extraction.url
            break
        if is_no_retry_reason(last_extraction.reason):
            console.print(
                f"  [yellow]구조적 실패 ({last_extraction.reason}) — 재시도 건너뜀[/yellow]"
            )
            break
        if attempt < _MAX_URL_RETRIES:
            console.print(f"  [yellow]URL 추출 실패 ({last_extraction.reason}). {_RETRY_WAIT}초 후 재시도합니다...[/yellow]")
            await asyncio.sleep(_RETRY_WAIT)

    if not video_url:
        # 세분화된 sub-reason 과 진단 컨텍스트를 그대로 progress_store/로그에 전달.
        extract_reason = (last_extraction.reason if last_extraction else None) or REASON_URL_EXTRACT_FAILED
        diag = last_extraction.diagnostics if last_extraction else {}
        console.print(f"  [bold red]오류:[/bold red] 영상 URL 추출 실패 ({extract_reason}, 3회 시도)")
        logger, log_path = get_error_logger("download")
        logger.info("강의: %s", lec.title)
        logger.info("URL: %s", safe_url(lec.full_url))
        logger.info("오류: 영상 URL 추출 실패 (3회 재시도 후에도 실패) — reason=%s", extract_reason)
        logger.info("진단: %s", diag)
        console.print(f"  [dim]로그 저장: {log_path}[/dim]")
        from src.notifier.telegram_notifier import notify_download_error

        dispatch_if_configured(
            notify_download_error,
            course_name=course.long_name,
            week_label=lec.week_label,
            lecture_title=lec.title,
        )
        return DownloadResult(ok=False, reason=extract_reason)

    # 3. 파일 경로 결정
    mp4_relpath = make_filepath(course.long_name, lec.week_label, lec.title)
    mp4_path = (Path(download_dir) / mp4_relpath).resolve()
    base_dir = Path(download_dir).resolve()
    if not mp4_path.is_relative_to(base_dir):
        console.print("  [bold red]오류:[/bold red] 잘못된 다운로드 경로가 감지되었습니다.")
        return DownloadResult(ok=False, reason=REASON_PATH_INVALID)

    if audio_only:
        final_path = mp4_path.with_suffix(".mp3")
    elif both:
        final_path = mp4_path  # mp4 + mp3 둘 다 저장
    else:
        final_path = mp4_path
    console.print(f"  [dim]저장 경로: {final_path}[/dim]")
    console.print()

    # 4. mp4 다운로드 + Progress bar
    progress = Progress(
        SpinnerColumn(),
        TextColumn("  [bold]{task.description}"),
        BarColumn(bar_width=36),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
        expand=False,
    )
    task_id = progress.add_task(lec.title[:40], total=None)

    try:
        with Live(progress, console=console, refresh_per_second=8):

            def on_progress(downloaded: int, total: int):
                progress.update(task_id, completed=downloaded, total=total)

            await download_video_with_browser(page, video_url, mp4_path, on_progress=on_progress)
    except Exception as e:
        console.print(f"  [bold red]다운로드 실패:[/bold red] {e}")
        logger, log_path = get_error_logger("download")
        logger.info("강의: %s", lec.title)
        logger.info("URL: %s", safe_url(lec.full_url))
        logger.info("영상 URL: %s", video_url)
        logger.error("다운로드 실패: %s", e, exc_info=True)
        console.print(f"  [dim]로그 저장: {log_path}[/dim]")
        from src.notifier.telegram_notifier import notify_download_error

        dispatch_if_configured(
            notify_download_error,
            course_name=course.long_name,
            week_label=lec.week_label,
            lecture_title=lec.title,
        )

        # 실패 사유 분류
        if isinstance(e, SSRFBlockedError):
            reason = REASON_SSRF_BLOCKED
        elif isinstance(e, SuspiciousStubError):
            reason = REASON_SUSPICIOUS_STUB
        elif isinstance(
            e,
            requests.exceptions.ConnectionError
            | requests.exceptions.Timeout
            | requests.exceptions.ChunkedEncodingError,
        ):
            reason = REASON_NETWORK
        else:
            reason = REASON_UNKNOWN
        return DownloadResult(ok=False, reason=reason)

    # 5-8. 후속 파이프라인 (mp3 변환 → STT → AI 요약 → 텔레그램 알림)
    # ARCH-001: TUI 와 WS(/download/pipeline) 가 동일한 run_pipeline 을 호출.
    # LOG-013/LOG-014: run_pipeline 은 내부에서 blocking(convert_to_mp3, transcribe,
    # summarize) 을 run_in_executor 로 위임하므로 이벤트 루프 block 이 해소된다.
    from src.service.download_pipeline import PipelineStage, run_pipeline

    def _on_progress(p) -> None:
        """run_pipeline 의 stage 전이를 Rich console 로 렌더."""
        if p.progress == 0.0:
            if p.stage == PipelineStage.CONVERT:
                console.print()
                console.print("  [dim]mp3 변환 중...[/dim]")
            elif p.stage == PipelineStage.TRANSCRIBE:
                console.print()
                console.print("  [dim]STT 변환 중... (시간이 걸릴 수 있습니다)[/dim]")
            elif p.stage == PipelineStage.SUMMARIZE:
                console.print()
                console.print("  [dim]AI 요약 중...[/dim]")
            elif p.stage == PipelineStage.NOTIFY:
                console.print()
                console.print("  [dim]텔레그램으로 요약 전송 중...[/dim]")
        elif p.progress == 1.0 and p.message:
            console.print(f"  [green]{p.message}[/green]")

    tg = Config.get_telegram_credentials()
    ai_agent = Config.AI_AGENT or "gemini"
    pipe_result = await run_pipeline(
        mp4_path=mp4_path,
        course_name=course.long_name,
        week_label=lec.week_label,
        lecture_title=lec.title,
        audio_only=audio_only,
        both=both,
        stt_enabled=Config.STT_ENABLED == "true",
        stt_model=Config.WHISPER_MODEL or "base",
        stt_language=Config.STT_LANGUAGE,
        ai_enabled=Config.AI_ENABLED == "true",
        ai_agent=ai_agent,
        ai_api_key=Config.GOOGLE_API_KEY if ai_agent == "gemini" else Config.OPENAI_API_KEY,
        ai_model=Config.GEMINI_MODEL if ai_agent == "gemini" else "",
        ai_extra_prompt=Config.SUMMARY_PROMPT_EXTRA,
        tg_token=tg[0] if tg and Config.TELEGRAM_ENABLED == "true" else "",
        tg_chat_id=tg[1] if tg and Config.TELEGRAM_ENABLED == "true" else "",
        tg_auto_delete=Config.TELEGRAM_AUTO_DELETE == "true",
        on_progress=_on_progress,
    )

    if not pipe_result.success and pipe_result.error == "CONVERT_FAILED":
        console.print(f"  [bold red]mp3 변환 실패:[/bold red] {pipe_result.stage_errors.get('convert', '')}")
        return DownloadResult(ok=False, reason=REASON_MP3_FAILED, mp4_path=mp4_path)

    # 최종 결과 요약
    console.print()
    console.print("  [bold green]다운로드 완료![/bold green]")
    if pipe_result.mp4_path:
        console.print(f"  [dim]{pipe_result.mp4_path}[/dim]")
    if pipe_result.mp3_path:
        console.print(f"  [dim]{pipe_result.mp3_path}[/dim]")
    if pipe_result.txt_path:
        console.print(f"  [dim]{pipe_result.txt_path}[/dim]")
    if pipe_result.summary_path:
        console.print(f"  [dim]{pipe_result.summary_path}[/dim]")
    if "transcribe" in pipe_result.stage_errors:
        console.print(f"  [bold red]STT 실패:[/bold red] {pipe_result.stage_errors['transcribe']}")
    if "summarize" in pipe_result.stage_errors:
        console.print(f"  [yellow]AI 요약:[/yellow] {pipe_result.stage_errors['summarize']}")
    if "notify" in pipe_result.stage_errors:
        console.print(f"  [yellow]텔레그램:[/yellow] {pipe_result.stage_errors['notify']}")

    return DownloadResult(
        ok=True,
        mp4_path=pipe_result.mp4_path if pipe_result.mp4_path and pipe_result.mp4_path.exists() else None,
        mp3_path=pipe_result.mp3_path,
        txt_path=pipe_result.txt_path,
        summary_path=pipe_result.summary_path,
    )
