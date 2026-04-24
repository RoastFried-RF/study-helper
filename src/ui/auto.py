"""
자동 모드 UI.

지정된 스케줄(KST 기준)마다 미시청 강의를 순차적으로
재생 → 다운로드 → STT → AI 요약 → 텔레그램 알림 처리한다.
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from rich.console import Console
from rich.prompt import Prompt

from src.config import KST, Config, get_data_path
from src.downloader.paths import file_present
from src.downloader.result import (
    REASON_PLAY_FAILED,
    REASON_STOPPED,
    REASON_UNKNOWN,
    REASON_UNSUPPORTED,
    is_no_retry_reason,
)
from src.logger import get_logger
from src.service.progress_store import ProgressStore
from src.service.scheduler import (
    DEFAULT_SCHEDULE_HOURS,
    check_auto_prerequisites,
    fmt_remaining,
    next_schedule_time,
    parse_schedule_input,
)
from src.ui._widgets import header_panel

if TYPE_CHECKING:
    from src.scraper.course_scraper import CourseScraper
    from src.scraper.models import Course, CourseDetail, LectureItem

console = Console()
_log = get_logger("auto")

# 자동 모드 진행 상태 파일
_PROGRESS_FILE = get_data_path("auto_progress.json")


@dataclass
class PlayResult:
    """_process_lecture 반환 타입.

    played: 재생(출석) 성공 여부
    downloaded: 파일 다운로드 성공 여부 (재생 스킵 경로 포함)
    downloadable: 구조적 다운로드 가능 여부 (learningx → False)
    reason: 실패 사유 (성공 시 None)
    """

    played: bool = False
    downloaded: bool = False
    downloadable: bool = True
    reason: str | None = None


class DownloadStepResult(NamedTuple):
    """`_run_download_step` 반환 — `(bool, str|None, bool)` 매직 튜플 대체 (TYPE-005)."""

    ok: bool
    reason: str | None
    downloadable: bool

# ARCH-010: 재시도 정책은 src.config.RetryPolicy 에서 단일 관리.
from src.config import RetryPolicy as _RetryPolicy  # noqa: E402

_MAX_PLAY_RETRIES = _RetryPolicy.PLAY
_BROWSER_RESTART_INTERVAL = _RetryPolicy.BROWSER_RESTART_INTERVAL

# B3: Playwright driver/browser 죽음을 감지하는 예외 메시지 패턴.
# 이 중 하나가 예외 메시지에 포함되면 scraper를 close() 후 start()로 재시작한다.
_DEAD_BROWSER_MARKERS = (
    "connection closed",                     # driver 연결 종료
    "target page, context or browser has been closed",
    "browser has been closed",
    "browsercontext has been closed",
    "browsercontext.new_page",               # 컨텍스트 자체가 죽었을 때 흔한 메시지
    "websocket.",                            # playwright 내부 ws 예외
)


def _is_browser_dead_exception(exc: BaseException) -> bool:
    """예외 메시지를 보고 Playwright 브라우저/드라이버 death 여부를 추정한다."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _DEAD_BROWSER_MARKERS)


async def _restart_browser_with_retry(scraper: "CourseScraper", max_retries: int = 3) -> bool:
    """브라우저를 close() 후 start()로 재시작한다. 실패 시 지수 백오프 재시도.

    Returns:
        True  — 재시작 성공
        False — max_retries 모두 실패
    """
    _log.info("브라우저 재시작 시도")
    try:
        await scraper.close()
    except Exception as close_e:
        _log.debug("브라우저 close 실패 (무시): %s", close_e)

    for retry in range(max_retries):
        try:
            await scraper.start()
            _log.info("브라우저 재시작 완료 (retry=%d)", retry + 1)
            return True
        except Exception as restart_e:
            _log.error("브라우저 재시작 실패 (%d/%d): %s", retry + 1, max_retries, restart_e)
            if retry < max_retries - 1:
                await asyncio.sleep(5 * (retry + 1))
            else:
                # 마지막 실패 시 부분 생성된 리소스 정리
                try:
                    await scraper.close()
                except Exception:
                    pass
    return False


async def _recover_if_browser_dead(
    scraper: "CourseScraper",
    exc: BaseException,
    context_msg: str,
) -> bool:
    """예외가 브라우저 death 패턴과 일치하면 재시작을 시도한다.

    Args:
        scraper:     재시작 대상 CourseScraper
        exc:         관찰된 예외
        context_msg: 로그/콘솔에 표시할 맥락 문자열 (예: 강의명)

    Returns:
        True  — 재시작 수행(성공/실패 무관), 호출 측은 이 강의 건너뛰고 다음으로 진행 권장
        False — 브라우저 죽음 패턴 아님. 예외 처리 계속 진행
    """
    if not _is_browser_dead_exception(exc):
        return False
    _log.warning("브라우저 연결 끊김 감지 (%s): %s", context_msg, exc)
    console.print(f"  [yellow]브라우저 연결 끊김 — 재시작 시도 ({context_msg})[/yellow]")
    ok = await _restart_browser_with_retry(scraper)
    if ok:
        console.print("  [dim]브라우저 재시작 완료[/dim]")
    else:
        console.print("  [red]브라우저 재시작 실패 — 이 사이클 후반 작업이 연쇄 실패할 수 있습니다[/red]")
    return True


def _load_store() -> ProgressStore:
    """ProgressStore를 로드한다 (v1 리스트 → v2 dict 자동 마이그레이션)."""
    store = ProgressStore(path=_PROGRESS_FILE)
    try:
        store.load()
    except Exception as e:
        _log.warning("auto_progress.json 로드 실패: %s", e)
    return store


def _save_store(store: ProgressStore) -> None:
    try:
        store.save()
    except Exception as e:
        _log.warning("auto_progress.json 저장 실패: %s", e)


def _is_file_present(course: "Course", lec: "LectureItem", rule: str) -> bool:
    """DOWNLOAD_RULE에 따라 기대되는 파일이 모두 존재하는지 확인한다."""
    return file_present(Config.get_download_dir(), course.long_name, lec, rule)


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


async def run_auto_mode(
    scraper: "CourseScraper",
    courses: list["Course"],
    details: list["CourseDetail | None"],
) -> None:
    """
    자동 모드 진입점.

    Args:
        scraper:  CourseScraper 인스턴스
        courses:  Course 목록
        details:  CourseDetail 목록 (courses와 동일 순서)
    """
    from src.ui.courses import _reload_details

    console.clear()

    # ── 필수 조건 체크 ────────────────────────────────────────────
    issues = check_auto_prerequisites(Config)
    if issues:
        console.print(header_panel("자동 모드"))
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
    console.print(header_panel("자동 모드"))
    console.print()
    console.print(f"  스케줄: KST {', '.join(f'{h:02d}:00' for h in schedule_hours)}")
    console.print()

    stop_event = asyncio.Event()

    async def _input_listener():
        """별도 태스크로 사용자 입력을 감시한다. '0' + Enter로 종료."""
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line.strip() == "0":
                    stop_event.set()
                    break
            except Exception:
                break

    listener_task = asyncio.create_task(_input_listener())
    listener_task.add_done_callback(
        lambda t: _log.debug("입력 리스너 종료: %s", t.exception()) if not t.cancelled() and t.exception() else None
    )
    cycle_count = 0

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
            cycle_count += 1
            now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            _log.info("스케줄 체크 시작 (cycle %d)", cycle_count)
            console.print(f"  [bold cyan][{now_str}] 스케줄 체크 시작[/bold cyan]")
            console.print()

            # 브라우저 메모리 누적 방지: N사이클마다 재시작
            if cycle_count > 1 and cycle_count % _BROWSER_RESTART_INTERVAL == 0:
                _log.info("브라우저 주기적 재시작 (cycle %d)", cycle_count)
                console.print("  [dim]브라우저 메모리 정리를 위해 재시작 중...[/dim]")
                if await _restart_browser_with_retry(scraper):
                    console.print("  [dim]브라우저 재시작 완료[/dim]")
                else:
                    console.print("  [red]브라우저 재시작 3회 실패 — 자동 모드를 종료합니다.[/red]")
                    stop_event.set()
                    break

            # 강의 목록 새로고침
            try:
                details = await _reload_details(scraper, courses)
            except Exception as e:
                _log.error("강의 목록 갱신 실패: %s", e)
                console.print(f"  [red]강의 목록 갱신 실패: {e}[/red]")
                # 브라우저 연결 끊김 감지 시 자동 재시작 (공용 헬퍼)
                await _recover_if_browser_dead(scraper, e, "강의 목록 갱신")
                await asyncio.sleep(60)
                continue

            # 마감 임박 항목 알림 체크
            tg = Config.get_telegram_credentials()
            if tg:
                from src.notifier.deadline_checker import check_and_notify_deadlines

                dl_count = check_and_notify_deadlines(courses, details, token=tg[0], chat_id=tg[1])
                if dl_count > 0:
                    console.print(f"  [yellow]마감 임박 항목 {dl_count}건 — 텔레그램 알림 전송[/yellow]")

            # ── 과목별 강의 수집 + pending 산출 ────────────────────
            # ProgressStore 기반:
            #   1. 재생 미완료 (needs_watch) → full pending (재생+다운로드)
            #   2. 재생은 완료됐지만 store.needs_download_retry → download-only pending
            #   3. 파일시스템이 이미 존재하면 store에 확정 기록 후 스킵
            store = _load_store()
            rule = Config.DOWNLOAD_RULE or "both"

            all_urls: set[str] = set()
            full_pending: list[tuple] = []
            dl_only_pending: list[tuple] = []
            total_videos = 0
            still_incomplete_urls: set[str] = set()

            for course, detail in zip(courses, details, strict=False):
                if detail is None:
                    continue
                for lec in detail.all_video_lectures:
                    total_videos += 1
                    all_urls.add(lec.full_url)

                    # 구조적으로 다운로드 불가능한 항목(learningx 등) — store에 불가로 표시
                    is_unsupported = not lec.is_downloadable

                    if lec.needs_watch:
                        # LMS 기준 아직 완료 안 된 것 → 풀 파이프라인 (store에 성공 기록이 있더라도 재시도)
                        if store.get(lec.full_url) and store.is_fully_done(lec.full_url):
                            still_incomplete_urls.add(lec.full_url)
                        full_pending.append((course, lec))
                        continue

                    # LMS 기준 완료. store 상태 확인
                    entry = store.get(lec.full_url)

                    if is_unsupported:
                        if entry is None or entry.downloadable is not False:
                            store.mark_unsupported(lec.full_url, reason=REASON_UNSUPPORTED)
                        continue

                    # 재생 완료로 확정
                    if entry is None or not entry.played:
                        store.mark_played(lec.full_url)

                    # 파일시스템 선점 확인 (외부 수동 다운로드 포함)
                    if _is_file_present(course, lec, rule):
                        store.mark_download_confirmed_from_filesystem(lec.full_url)
                        continue

                    # 아직 파일이 없으면 download-only pending
                    dl_only_pending.append((course, lec))

            # ── 정리: LMS가 여전히 미완료로 보는 항목은 store에서 played 해제 ──
            for url in still_incomplete_urls:
                store.mark_incomplete(url)

            # ── 정리: 현재 LMS에 존재하는 URL만 유지 ───────────────
            orphan_count = store.retain_only(all_urls)

            _save_store(store)
            if still_incomplete_urls:
                console.print(
                    f"  [dim]이전 처리 후 LMS 미완료 재전환 {len(still_incomplete_urls)}건 — 재시도 대상[/dim]"
                )
            if orphan_count:
                _log.info("progress orphan 정리: %d건", orphan_count)

            stats_msg = (
                f"전체 비디오 {total_videos}개 / 풀 대상 {len(full_pending)}개 "
                f"/ 다운로드만 {len(dl_only_pending)}개 / store {len(store.entries)}개"
            )
            _log.info(stats_msg)
            console.print(f"  [dim]{stats_msg}[/dim]")

            if not full_pending and not dl_only_pending:
                console.print("  [dim]처리할 강의가 없습니다.[/dim]")
                console.print()
                continue

            if full_pending:
                console.print(f"  풀 처리 대상 [bold]{len(full_pending)}개[/bold]")
            if dl_only_pending:
                console.print(f"  다운로드만 재시도 [bold]{len(dl_only_pending)}개[/bold]")
            console.print()

            # ── 1단계: 재생+다운로드 풀 파이프라인 ──────────────────
            for course, lec in full_pending:
                if stop_event.is_set():
                    break
                result = await _process_lecture(scraper, course, lec, stop_event)
                _apply_play_result(store, lec.full_url, result)
                _save_store(store)

            # ── 2단계: 재생 스킵 + 다운로드만 재시도 ────────────────
            for course, lec in dl_only_pending:
                if stop_event.is_set():
                    break
                result = await _process_download_only(scraper, course, lec)
                _apply_play_result(store, lec.full_url, result)
                _save_store(store)

            # ── 다운로드 누락 점검 (파일시스템 기준 재검증, CQS 분리) ─────
            _reconcile_store_with_filesystem(courses, details, store)
            _save_store(store)
            missing_entries = _list_missing_entries(courses, details)
            _notify_download_gaps(missing_entries)

            # STT 모델 메모리 해제 (다음 사이클까지 필요 없음)
            from src.stt.transcriber import safe_unload

            safe_unload()

            console.print()
            console.print("  [bold green]이번 스케줄 처리 완료.[/bold green]")
            console.print()

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n  [dim]자동 모드 중단...[/dim]")
    finally:
        listener_task.cancel()
        try:
            await listener_task
        except (asyncio.CancelledError, Exception):
            pass
        # STT 모델 메모리 최종 해제
        from src.stt.transcriber import safe_unload

        safe_unload()

    console.print()
    console.print("  [dim]자동 모드를 종료합니다.[/dim]")
    console.print()


async def _process_lecture(
    scraper: "CourseScraper",
    course: "Course",
    lec: "LectureItem",
    stop_event: asyncio.Event,
) -> PlayResult:
    """
    단일 강의 풀 파이프라인: 재생 → 다운로드 → STT → AI 요약 → 텔레그램 알림.
    오류 발생 시 텔레그램으로 알림을 보내고 다음 강의로 넘어간다.

    Returns:
        PlayResult: played/downloaded/downloadable/reason
    """
    from src.ui.player import run_player

    label = f"[{course.long_name}] {lec.title}"
    now_str = datetime.now(KST).strftime("%H:%M:%S")
    _log.info("처리 시작: %s", label)
    console.print(f"  [{now_str}] [bold]{label}[/bold] 처리 중...")

    # ── 세션 유효성 체크 ─────────────────────────────────────────
    try:
        await scraper.ensure_session()
    except Exception as e:
        _log.warning("세션 확인 오류: %s (계속 시도)", e)

    # ── 재생 (최대 3회 재시도) ──────────────────────────────────────
    play_success = False
    last_err_msg = ""
    for play_attempt in range(1, _MAX_PLAY_RETRIES + 1):
        if stop_event.is_set():
            return PlayResult(played=False, reason=REASON_STOPPED)
        if play_attempt > 1:
            wait_sec = 5 * play_attempt  # 10s, 15s
            _log.info("재생 재시도 %d/%d (%d초 대기): %s", play_attempt, _MAX_PLAY_RETRIES, wait_sec, label)
            console.print(f"  [dim]  → 재생 재시도 {play_attempt}/{_MAX_PLAY_RETRIES} ({wait_sec}초 대기)...[/dim]")
            await asyncio.sleep(wait_sec)
            try:
                await scraper.ensure_session()
            except Exception:
                pass
        else:
            console.print("  [dim]  → 재생 중...[/dim]")

        try:
            success, has_error = await run_player(scraper.page, lec)
            if success:
                play_success = True
                break
            last_err_msg = "재생 오류" if has_error else "재생 미완료"
            _log.warning("재생 실패 (%d/%d): %s — %s", play_attempt, _MAX_PLAY_RETRIES, label, last_err_msg)
            console.print(f"  [yellow]  → {last_err_msg} ({play_attempt}/{_MAX_PLAY_RETRIES})[/yellow]")
        except Exception as e:
            # SEC-101: 원본 예외 str에는 세션 토큰 포함 URL이 섞일 수 있어
            # 사용자/텔레그램 노출용 메시지는 타입명만 쓴다. 상세는 study_helper.log에만.
            exc_type = type(e).__name__
            last_err_msg = f"재생 실패: {exc_type}"
            _log.error("재생 예외 (%d/%d): %s — %s", play_attempt, _MAX_PLAY_RETRIES, label, e, exc_info=True)
            console.print(f"  [red]  → {last_err_msg} ({play_attempt}/{_MAX_PLAY_RETRIES})[/red]")
            # B3: 브라우저가 죽은 경우 재시작 후 다음 attempt로 자연스럽게 진행
            await _recover_if_browser_dead(scraper, e, label)

    if not play_success:
        _log.warning("재생 최종 실패: %s — %s", label, last_err_msg)
        _tg_error_notify(course, lec, f"{last_err_msg} ({_MAX_PLAY_RETRIES}회 재시도 후 실패)")
        return PlayResult(played=False, reason=REASON_PLAY_FAILED)

    lec.completion = "completed"
    _log.info("재생 완료: %s", label)
    console.print("  [dim]  → 재생 완료[/dim]")

    if stop_event.is_set():
        return PlayResult(played=True, downloaded=False, reason=REASON_STOPPED)

    # ── 다운로드 ──────────────────────────────────────────────────
    step = await _run_download_step(scraper, course, lec, label)
    download_ok, reason, downloadable = step.ok, step.reason, step.downloadable
    if download_ok:
        console.print(f"  [bold green]  → {label} 완료[/bold green]")
        console.print()
    return PlayResult(
        played=True,
        downloaded=download_ok,
        downloadable=downloadable,
        reason=reason,
    )


async def _process_download_only(
    scraper: "CourseScraper",
    course: "Course",
    lec: "LectureItem",
) -> PlayResult:
    """재생 스킵, 다운로드만 재시도하는 fast-path.

    store에 재생 완료로 기록되어 있고 파일만 누락된 경우 사용된다.
    """
    label = f"[{course.long_name}] {lec.title}"
    _log.info("다운로드 재시도(재생 스킵): %s", label)
    console.print(f"  [dim]  → 다운로드 재시도: [bold]{label}[/bold][/dim]")

    try:
        await scraper.ensure_session()
    except Exception as e:
        _log.warning("세션 확인 오류: %s (계속 시도)", e)

    step = await _run_download_step(scraper, course, lec, label)
    download_ok, reason, downloadable = step.ok, step.reason, step.downloadable
    return PlayResult(
        played=True,  # 이미 완료된 상태라는 전제
        downloaded=download_ok,
        downloadable=downloadable,
        reason=reason,
    )


_MAX_DOWNLOAD_RETRIES = _RetryPolicy.DOWNLOAD


async def _run_download_step(
    scraper: "CourseScraper",
    course: "Course",
    lec: "LectureItem",
    label: str,
) -> DownloadStepResult:
    """`run_download`를 최대 3회 시도하고 결과를 `DownloadStepResult`로 반환한다.

    B7: 브라우저 죽음/일시적 네트워크 오류로 한 번 실패해도 같은 강의를 즉시
    포기하지 않도록 3회 지수 백오프 재시도. 재시도 전에 브라우저 death 감지 시
    자동 재시작을 수행한다. UNSUPPORTED/SUSPICIOUS_STUB 등 구조적 실패는
    재시도해도 무의미하므로 즉시 반환한다.
    """
    from src.ui.download import run_download

    rule = Config.DOWNLOAD_RULE or "both"
    audio_only = rule == "audio"
    both = rule == "both"

    last_reason = REASON_UNKNOWN
    last_exc_type = ""
    for attempt in range(1, _MAX_DOWNLOAD_RETRIES + 1):
        if attempt == 1:
            _log.info("다운로드 시작: %s", label)
            console.print("  [dim]  → 다운로드 중...[/dim]")
        else:
            wait_sec = 5 * (attempt - 1)  # 5s, 10s
            _log.info("다운로드 재시도 %d/%d (%d초 대기): %s", attempt, _MAX_DOWNLOAD_RETRIES, wait_sec, label)
            console.print(f"  [dim]  → 다운로드 재시도 {attempt}/{_MAX_DOWNLOAD_RETRIES} ({wait_sec}초 대기)...[/dim]")
            await asyncio.sleep(wait_sec)

        try:
            result = await run_download(scraper.page, lec, course, audio_only=audio_only, both=both)
        except Exception as e:
            # SEC-002: 원본 예외 메시지에는 세션 토큰 포함 URL이 섞일 수 있어
            # 텔레그램 등 외부 채널에는 타입명만 보낸다. 상세는 study_helper.log에만.
            exc_type = type(e).__name__
            last_exc_type = exc_type
            last_reason = f"exception:{exc_type}"
            _log.error(
                "다운로드 예외 (%d/%d): %s — %s", attempt, _MAX_DOWNLOAD_RETRIES, label, e, exc_info=True,
            )
            console.print(f"  [red]  → 다운로드 실패: {exc_type} ({attempt}/{_MAX_DOWNLOAD_RETRIES})[/red]")
            # 브라우저 죽음 감지 시 재시작 — 다음 attempt에 정상 페이지 사용
            await _recover_if_browser_dead(scraper, e, label)
            continue

        if result.ok:
            _log.info("다운로드 완료: %s", label)
            console.print("  [dim]  → 다운로드 완료[/dim]")
            return DownloadStepResult(ok=True, reason=None, downloadable=True)

        # 재시도해도 무의미한 구조적 실패는 즉시 반환. 재시도 정책은
        # result.is_no_retry_reason() 에 중앙집중 — 새 reason 추가 시 auto.py 를
        # 고칠 필요 없이 result.py 의 _NO_RETRY_REASONS 에만 추가하면 된다.
        if is_no_retry_reason(result.reason):
            _log.warning("다운로드 실패 (재시도 불가): %s — reason=%s", label, result.reason)
            console.print(f"  [yellow]  → 다운로드 실패: {label} (사유={result.reason})[/yellow]")
            downloadable = result.reason != REASON_UNSUPPORTED
            return DownloadStepResult(ok=False, reason=result.reason, downloadable=downloadable)

        last_reason = result.reason
        _log.warning(
            "다운로드 실패 (%d/%d): %s — reason=%s", attempt, _MAX_DOWNLOAD_RETRIES, label, result.reason,
        )
        console.print(
            f"  [yellow]  → 다운로드 실패: {label} (사유={result.reason}, {attempt}/{_MAX_DOWNLOAD_RETRIES})[/yellow]"
        )

    # 모든 재시도 실패
    if last_exc_type:
        _tg_error_notify(course, lec, f"다운로드 실패: {last_exc_type}")
    return DownloadStepResult(ok=False, reason=last_reason, downloadable=True)


def _apply_play_result(store: ProgressStore, url: str, result: PlayResult) -> None:
    """_process_lecture 결과를 ProgressStore에 반영한다."""
    if result.played:
        store.mark_played(url)
    if not result.downloadable:
        store.mark_unsupported(url, reason=result.reason or REASON_UNSUPPORTED)
        return
    if result.downloaded:
        store.mark_download_success(url)
    elif result.played:
        store.mark_download_failed(url, reason=result.reason or "unknown")


# ── 다운로드 누락 점검 — Command/Query/Side-effect 분리 (ARCH-011) ──
#
# 세 가지 책임을 각자 독립된 함수로 분리한다:
#   1. reconcile_store_with_filesystem  — Command. 파일시스템을 관찰해 store를 정정
#   2. list_missing_entries             — Query.   누락 목록만 반환(부수효과 없음)
#   3. _notify_download_gaps            — Side-effect. 콘솔/로그/텔레그램 발송
#
# `auto.py` 루프는 이 세 함수를 순서대로 호출해서 예전과 동일한 결과를 얻는다.
# `recover_pipeline.collect_missing`은 독립된 공용 수집기 — TUI/CLI recover용. 이쪽과
# `list_missing_entries`는 의도적으로 별도 유지한다(auto 루프는 튜플 포맷이 필요하고
# recover_pipeline은 MissingItem dataclass가 필요).


_MissingTuple = tuple[str, str, str, str]  # (course_long_name, week_label, title, kind)


def _reconcile_store_with_filesystem(
    courses: list["Course"],
    details: list["CourseDetail | None"],
    store: ProgressStore,
) -> None:
    """파일시스템 관찰 결과를 store에 반영한다 (Command)."""
    from src.service.download_state import reconcile_store_with_filesystem

    reconcile_store_with_filesystem(
        courses, details, store,
        download_dir=Config.get_download_dir(),
        rule=Config.DOWNLOAD_RULE or "both",
    )


def _list_missing_entries(
    courses: list["Course"],
    details: list["CourseDetail | None"],
) -> list[_MissingTuple]:
    """시청 완료된 강의 중 파일이 누락된 항목 튜플 목록을 반환한다 (Query, 부수효과 없음)."""
    from src.service.download_state import list_missing_items

    items = list_missing_items(
        courses, details,
        download_dir=Config.get_download_dir(),
        rule=Config.DOWNLOAD_RULE or "both",
    )
    return [(m.course.long_name, m.lec.week_label, m.lec.title, m.kind) for m in items]


def _notify_download_gaps(missing: list[_MissingTuple]) -> None:
    """콘솔 출력 + 로그 기록 + 텔레그램 알림 (Side-effect)."""
    if not missing:
        return
    rule = Config.DOWNLOAD_RULE or "both"
    console.print()
    console.print(f"  [yellow]다운로드 누락 {len(missing)}건 감지:[/yellow]")
    for course_name, week, title, ftype in missing[:10]:
        console.print(f"  [dim]  → [{course_name}] {week} {title} ({ftype})[/dim]")
    if len(missing) > 10:
        console.print(f"  [dim]  → ... 외 {len(missing) - 10}건[/dim]")
    _log.warning("다운로드 누락 %d건 감지 (rule=%s)", len(missing), rule)
    for course_name, week, title, ftype in missing:
        _log.warning("  · [%s] %s %s (누락=%s)", course_name, week, title, ftype)

    from src.notifier.telegram_dispatch import dispatch_if_configured
    from src.notifier.telegram_notifier import notify_download_gaps

    dispatch_if_configured(notify_download_gaps, missing=missing)


def _tg_error_notify(course: "Course", lec: "LectureItem", error_msg: str) -> None:
    """자동 모드 처리 오류를 텔레그램으로 알린다."""
    from src.notifier.telegram_dispatch import dispatch_if_configured
    from src.notifier.telegram_notifier import notify_auto_error

    dispatch_if_configured(
        notify_auto_error,
        course_name=course.long_name,
        week_label=lec.week_label,
        lecture_title=lec.title,
        error_msg=error_msg,
    )
