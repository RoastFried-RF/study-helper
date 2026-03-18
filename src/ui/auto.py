"""
자동 모드 UI.

지정된 스케줄(KST 기준)마다 미시청 강의를 순차적으로
재생 → 다운로드 → STT → AI 요약 → 텔레그램 알림 처리한다.
"""

import asyncio
import json
import sys
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from src.config import KST, Config, get_data_path
from src.service.scheduler import (
    DEFAULT_SCHEDULE_HOURS,
    check_auto_prerequisites,
    fmt_remaining,
    next_schedule_time,
    parse_schedule_input,
)

console = Console()

# 자동 모드 진행 상태 파일
_PROGRESS_FILE = get_data_path("auto_progress.json")


def _load_progress() -> set[str]:
    """처리 완료된 강의 URL 목록을 로드한다."""
    try:
        if _PROGRESS_FILE.exists():
            return set(json.loads(_PROGRESS_FILE.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        print("  [경고] auto_progress.json 파싱 실패 — 초기화합니다.", file=sys.stderr)
    except Exception:
        pass
    return set()


def _save_progress(completed: set[str]) -> None:
    """처리 완료된 강의 URL 목록을 저장한다."""
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROGRESS_FILE.write_text(json.dumps(sorted(completed)), encoding="utf-8")
    except Exception as e:
        print(f"  [경고] auto_progress.json 저장 실패: {e}", file=sys.stderr)


def _check_auto_prerequisites() -> list[str]:
    """자동 모드 필수 조건을 확인하고 미충족 항목 목록을 반환한다."""
    return check_auto_prerequisites(Config)


def _configure_schedule() -> list[int]:
    """
    스케줄 설정 UI를 표시하고 선택된 시각 목록을 반환한다.
    Enter를 누르면 기본값(09/13/18/23시)을 사용한다.
    """
    console.print()
    console.print("  [bold]자동 모드 스케줄 설정[/bold]")
    console.print()
    console.print(f"  기본 스케줄: KST 기준 {', '.join(f'{h:02d}:00' for h in DEFAULT_SCHEDULE_HOURS)}")
    console.print("  [dim]변경하려면 시간을 쉼표로 구분해 입력하세요. (예: 8,12,18,22)[/dim]")
    console.print("  [dim]Enter를 누르면 기본 스케줄을 사용합니다.[/dim]")
    console.print()

    while True:
        raw = Prompt.ask("  스케줄 입력", default="").strip()
        result = parse_schedule_input(raw)
        if result is not None:
            return result
        console.print("  [red]0~23 사이의 숫자를 쉼표로 구분해 입력하세요.[/red]")


async def run_auto_mode(scraper, courses, details) -> None:
    """
    자동 모드 진입점.

    Args:
        scraper:  CourseScraper 인스턴스
        courses:  Course 목록
        details:  CourseDetail 목록 (courses와 동일 순서)
    """
    console.clear()

    # ── 필수 조건 체크 ────────────────────────────────────────────
    issues = _check_auto_prerequisites()
    if issues:
        console.print(
            Panel(
                Text("자동 모드", justify="center", style="bold cyan"),
                border_style="cyan",
                padding=(0, 4),
            )
        )
        console.print()
        console.print("  [bold yellow]자동 모드 실행을 위한 필수 조건이 만족하지 않았습니다.[/bold yellow]")
        console.print()
        for issue in issues:
            console.print(f"  [red]✗[/red] {issue}")
        console.print()
        go_settings = Prompt.ask(
            "  설정 페이지로 이동하시겠습니까?", choices=["y", "n"], default="y", show_choices=True
        )
        if go_settings == "y":
            from src.ui.settings import run_settings

            run_settings()
        return

    # ── 스케줄 설정 ───────────────────────────────────────────────
    schedule_hours = _configure_schedule()
    run_now = Prompt.ask("  즉시 실행할까요?", choices=["y", "n"], default="y", show_choices=True).strip() == "y"

    # ── 자동 모드 루프 ────────────────────────────────────────────
    console.clear()
    console.print(
        Panel(
            Text("자동 모드", justify="center", style="bold cyan"),
            border_style="cyan",
            padding=(0, 4),
        )
    )
    console.print()
    console.print(f"  스케줄: KST {', '.join(f'{h:02d}:00' for h in schedule_hours)}")
    console.print()

    stop_event = asyncio.Event()

    async def _input_listener():
        """별도 태스크로 사용자 입력을 감시한다. '0' + Enter로 종료."""
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line.strip() == "0":
                    stop_event.set()
                    break
            except Exception:
                break

    listener_task = asyncio.create_task(_input_listener())

    try:
        while not stop_event.is_set():
            if run_now:
                # 첫 실행 시 대기 없이 바로 진행
                run_now = False
            else:
                next_time = next_schedule_time(schedule_hours)

                # 안내 줄 출력 (한 번만)
                sys.stdout.write("  0 + Enter 로 종료\n")
                sys.stdout.flush()

                # 대기 루프 — \r로 같은 줄 덮어쓰기
                while not stop_event.is_set():
                    now = datetime.now(KST)
                    if now >= next_time:
                        break
                    remaining = fmt_remaining(next_time)
                    line = (
                        f"  \033[1;32m● 자동 모드 동작 중\033[0m"
                        f"  \033[2m다음 체크  {next_time.strftime('%H:%M')} ({remaining} 후)\033[0m"
                        "          "
                    )
                    sys.stdout.write(f"\r{line}")
                    sys.stdout.flush()
                    await asyncio.sleep(1)

                # 상태 줄 정리 후 개행
                sys.stdout.write("\r" + " " * 80 + "\r\n")
                sys.stdout.flush()

                if stop_event.is_set():
                    break

            console.print()
            now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            console.print(f"  [bold cyan][{now_str}] 스케줄 체크 시작[/bold cyan]")
            console.print()

            # 강의 목록 새로고침
            try:
                from src.ui.courses import _reload_details

                details = await _reload_details(scraper, courses)
            except Exception as e:
                console.print(f"  [red]강의 목록 갱신 실패: {e}[/red]")
                await asyncio.sleep(60)
                continue

            # 마감 임박 항목 알림 체크
            tg = Config.get_telegram_credentials()
            if tg:
                from src.notifier.deadline_checker import check_and_notify_deadlines

                dl_count = check_and_notify_deadlines(courses, details, token=tg[0], chat_id=tg[1])
                if dl_count > 0:
                    console.print(f"  [yellow]마감 임박 항목 {dl_count}건 — 텔레그램 알림 전송[/yellow]")

            # 과목별 미시청 강의 수집
            # LMS에서 여전히 미시청인 강의는 progress에서 제거하여 재시도
            completed = _load_progress()
            still_incomplete: set[str] = set()
            all_needs_watch: list[tuple] = []
            total_videos = 0
            for course, detail in zip(courses, details, strict=False):
                if detail is None:
                    continue
                for lec in detail.all_video_lectures:
                    total_videos += 1
                    if lec.needs_watch:
                        if lec.full_url in completed:
                            still_incomplete.add(lec.full_url)
                        all_needs_watch.append((course, lec))

            # LMS가 여전히 미완료로 보는 항목은 progress에서 제거
            stale = completed & still_incomplete
            if stale:
                completed -= stale
                _save_progress(completed)
                console.print(f"  [dim]이전 처리 후 미완료 {len(stale)}건 재시도 대상[/dim]")

            # progress에 없는 (아직 미처리) 강의만 대상
            pending_list = [
                (c, l) for c, l in all_needs_watch if l.full_url not in completed
            ]

            console.print(
                f"  [dim]전체 비디오 {total_videos}개 / 미시청 {len(all_needs_watch)}개 "
                f"/ progress {len(completed)}개 / 대상 {len(pending_list)}개[/dim]"
            )

            if not pending_list:
                console.print("  [dim]미시청 강의가 없습니다.[/dim]")
                console.print()
                continue

            console.print(f"  미시청 강의 [bold]{len(pending_list)}개[/bold] 발견. 순차 처리 시작합니다.")
            console.print()

            for course, lec in pending_list:
                if stop_event.is_set():
                    break
                success = await _process_lecture(scraper, course, lec, stop_event)
                if success:
                    completed.add(lec.full_url)
                    _save_progress(completed)

            console.print()
            console.print("  [bold green]이번 스케줄 처리 완료.[/bold green]")
            console.print()

    finally:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

    console.print()
    console.print("  [dim]자동 모드를 종료합니다.[/dim]")
    console.print()


async def _process_lecture(scraper, course, lec, stop_event: asyncio.Event) -> bool:
    """
    단일 강의에 대해 재생 → 다운로드 → STT → AI 요약 → 텔레그램 알림을 처리한다.
    오류 발생 시 텔레그램으로 알림을 보내고 다음 강의로 넘어간다.

    Returns:
        True: 재생+다운로드 모두 성공 / False: 실패
    """
    from src.ui.download import run_download
    from src.ui.player import run_player

    label = f"[{course.long_name}] {lec.title}"
    now_str = datetime.now(KST).strftime("%H:%M:%S")
    console.print(f"  [{now_str}] [bold]{label}[/bold] 처리 중...")

    # ── 재생 ──────────────────────────────────────────────────────
    console.print("  [dim]  → 재생 중...[/dim]")
    try:
        success, has_error = await run_player(scraper._page, lec)
        if not success:
            err_msg = "재생 오류" if has_error else "재생 미완료"
            console.print(f"  [yellow]  → {err_msg}: {label}[/yellow]")
            _tg_error_notify(course, lec, err_msg)
            return False
        lec.completion = "completed"
        console.print("  [dim]  → 재생 완료[/dim]")
    except Exception as e:
        console.print(f"  [red]  → 재생 실패: {e}[/red]")
        _tg_error_notify(course, lec, f"재생 실패: {e}")
        return False

    if stop_event.is_set():
        return False

    # ── 다운로드 ──────────────────────────────────────────────────
    rule = Config.DOWNLOAD_RULE or "both"
    audio_only = rule == "audio"
    both = rule == "both"
    console.print("  [dim]  → 다운로드 중...[/dim]")
    try:
        ok = await run_download(scraper._page, lec, course, audio_only=audio_only, both=both)
        if not ok:
            console.print(f"  [yellow]  → 다운로드 실패: {label}[/yellow]")
            # run_download 내부에서 이미 텔레그램 알림 처리됨
            return False
        console.print("  [dim]  → 다운로드 완료[/dim]")
    except Exception as e:
        console.print(f"  [red]  → 다운로드 실패: {e}[/red]")
        _tg_error_notify(course, lec, f"다운로드 실패: {e}")
        return False

    console.print(f"  [bold green]  → {label} 완료[/bold green]")
    console.print()
    return True


def _tg_error_notify(course, lec, error_msg: str) -> None:
    """자동 모드 처리 오류를 텔레그램으로 알린다."""
    creds = Config.get_telegram_credentials()
    if not creds:
        return
    try:
        from src.notifier.telegram_notifier import notify_auto_error

        notify_auto_error(creds[0], creds[1], course.long_name, lec.week_label, lec.title, error_msg)
    except Exception:
        pass
