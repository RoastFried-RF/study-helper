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
    TimeElapsedColumn,
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
    from src.converter.audio_converter import convert_to_mp3
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

    def _tg_error(msg_fn):
        """텔레그램 오류 알림을 전송한다 (설정된 경우에만)."""
        creds = Config.get_telegram_credentials()
        if creds:
            msg_fn(creds[0], creds[1])

    # 1. 구조적으로 다운로드 불가능한 항목 조기 감지 (learningx 등)
    if not lec.is_downloadable:
        console.print("  [yellow]다운로드 불가:[/yellow] 이 강의는 다운로드가 지원되지 않는 형식입니다.")
        from src.notifier.telegram_notifier import notify_download_unsupported

        _tg_error(lambda t, c: notify_download_unsupported(t, c, course.long_name, lec.week_label, lec.title))
        return DownloadResult(ok=False, reason=REASON_UNSUPPORTED)

    # 2. video URL 추출 (최대 3회 재시도)
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

        _tg_error(lambda t, c: notify_download_error(t, c, course.long_name, lec.week_label, lec.title))
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

        _tg_error(lambda t, c: notify_download_error(t, c, course.long_name, lec.week_label, lec.title))

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

    # 5. mp3 변환 (audio_only 또는 both)
    mp3_path: Path | None = None
    if audio_only or both:
        console.print()
        console.print("  [dim]mp3 변환 중...[/dim]")
        try:
            mp3_path = convert_to_mp3(mp4_path)
            if audio_only:
                mp4_path.unlink()  # 음성 전용: 원본 mp4 삭제
        except Exception as e:
            console.print(f"  [bold red]mp3 변환 실패:[/bold red] {e}")
            return DownloadResult(ok=False, reason=REASON_MP3_FAILED, mp4_path=mp4_path)

        console.print()
        console.print("  [bold green]다운로드 완료![/bold green]")
        if both:
            console.print(f"  [dim]{mp4_path}[/dim]")
        console.print(f"  [dim]{mp3_path}[/dim]")
    else:
        console.print()
        console.print("  [bold green]다운로드 완료![/bold green]")
        console.print(f"  [dim]{mp4_path}[/dim]")

    # 6. STT 변환 (mp3가 있고 STT_ENABLED=true인 경우)
    txt_path = None
    if mp3_path and Config.STT_ENABLED == "true":
        console.print()
        console.print("  [dim]STT 변환 중... (시간이 걸릴 수 있습니다)[/dim]")
        try:
            from src.stt.transcriber import transcribe, unload_model

            txt_path = transcribe(
                mp3_path,
                model_size=Config.WHISPER_MODEL or "base",
                language=Config.STT_LANGUAGE,
            )
            console.print("  [bold green]STT 완료![/bold green]")
            console.print(f"  [dim]{txt_path}[/dim]")
        except Exception as e:
            console.print(f"  [bold red]STT 실패:[/bold red] {e}")
        finally:
            try:
                unload_model()
            except Exception:
                pass

    # 7. AI 요약 (txt가 있고 AI_ENABLED=true인 경우)
    summary_path = None
    if txt_path and Config.AI_ENABLED == "true":
        # B4: STT 결과가 비어 있으면 요약 호출 생략
        from src.stt.transcriber import is_transcript_usable

        if not is_transcript_usable(txt_path):
            console.print("  [yellow]AI 요약 건너뜀: STT 결과가 비어 있습니다 (무음/저음량 영상 가능).[/yellow]")
        else:
            api_key = Config.GOOGLE_API_KEY if Config.AI_AGENT == "gemini" else Config.OPENAI_API_KEY
            model = Config.GEMINI_MODEL if Config.AI_AGENT == "gemini" else ""
            if not api_key:
                console.print("  [yellow]AI 요약 건너뜀: API 키가 설정되지 않았습니다.[/yellow]")
            else:
                import warnings

                from src.summarizer.summarizer import GEMINI_DEFAULT_MODEL, summarize

                console.print()
                spinner_progress = Progress(
                    SpinnerColumn(),
                    TextColumn("  [bold]{task.description}"),
                    TimeElapsedColumn(),
                    console=console,
                    expand=False,
                )
                task_id = spinner_progress.add_task("AI 요약 중...", total=None)

                try:
                    with Live(spinner_progress, console=console, refresh_per_second=8):
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            loop = asyncio.get_running_loop()
                            summary_path = await loop.run_in_executor(
                                None,
                                lambda: summarize(
                                    txt_path,
                                    agent=Config.AI_AGENT or "gemini",
                                    api_key=api_key,
                                    model=model or GEMINI_DEFAULT_MODEL,
                                    extra_prompt=Config.SUMMARY_PROMPT_EXTRA,
                                ),
                            )
                    console.print("  [bold green]AI 요약 완료![/bold green]")
                    console.print(f"  [dim]{summary_path}[/dim]")
                except Exception as e:
                    console.print(f"  [bold red]AI 요약 실패:[/bold red] {e}")

    # 8. 텔레그램 알림 (AI 요약 완료 시)
    if summary_path and Config.TELEGRAM_ENABLED == "true":
        tg_token = Config.TELEGRAM_BOT_TOKEN
        tg_chat_id = Config.TELEGRAM_CHAT_ID
        if tg_token and tg_chat_id:
            from src.notifier.telegram_notifier import notify_summary_complete, notify_summary_send_error

            console.print()
            console.print("  [dim]텔레그램으로 요약 전송 중...[/dim]")

            summary_text = summary_path.read_text(encoding="utf-8").strip()

            # 자동 삭제 대상 파일 목록
            files_to_delete = None
            if Config.TELEGRAM_AUTO_DELETE == "true":
                files_to_delete = [f for f in [mp4_path, mp3_path, txt_path, summary_path] if f]

            ok = notify_summary_complete(
                bot_token=tg_token,
                chat_id=tg_chat_id,
                course_name=course.long_name,
                week_label=lec.week_label,
                lecture_title=lec.title,
                summary_text=summary_text,
                summary_path=summary_path,
                auto_delete_files=files_to_delete,
            )
            if ok:
                console.print("  [bold green]텔레그램 전송 완료![/bold green]")
                if files_to_delete:
                    console.print("  [dim]파일이 자동 삭제되었습니다.[/dim]")
            else:
                console.print("  [yellow]텔레그램 전송 실패. 파일은 유지됩니다.[/yellow]")
                notify_summary_send_error(tg_token, tg_chat_id, course.long_name, lec.week_label, lec.title)

    return DownloadResult(
        ok=True,
        mp4_path=mp4_path if mp4_path.exists() else None,
        mp3_path=mp3_path,
        txt_path=txt_path,
        summary_path=summary_path,
    )
