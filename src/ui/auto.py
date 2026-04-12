"""
자동 모드 UI.

지정된 스케줄(KST 기준)마다 미시청 강의를 순차적으로
재생 → 다운로드 → STT → AI 요약 → 텔레그램 알림 처리한다.
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from src.config import KST, Config, get_data_path
from src.downloader.result import REASON_UNSUPPORTED
from src.logger import get_logger
from src.service.progress_store import ProgressStore
from src.service.scheduler import (
    DEFAULT_SCHEDULE_HOURS,
    check_auto_prerequisites,
    fmt_remaining,
    next_schedule_time,
    parse_schedule_input,
)

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

# 재생 재시도 설정
_MAX_PLAY_RETRIES = 3

# 브라우저 메모리 누적 방지: N사이클마다 브라우저 재시작
_BROWSER_RESTART_INTERVAL = 3


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


def _mp4_path_for(course_long_name: str, week_label: str, title: str) -> Path:
    from src.downloader.video_downloader import make_filepath

    download_dir = Config.get_download_dir()
    mp4_rel = make_filepath(course_long_name, week_label, title)
    return (Path(download_dir) / mp4_rel).resolve()


def _is_file_present(course, lec, rule: str) -> bool:
    """DOWNLOAD_RULE에 따라 기대되는 파일이 모두 존재하는지 확인한다."""
    mp4 = _mp4_path_for(course.long_name, lec.week_label, lec.title)
    mp3 = mp4.with_suffix(".mp3")
    if rule == "video":
        return mp4.exists()
    if rule == "audio":
        return mp3.exists()
    # "both" 또는 미설정
    return mp4.exists() and mp3.exists()


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
    from src.ui.courses import _reload_details

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
                try:
                    await scraper.close()
                except Exception as close_e:
                    _log.debug("브라우저 close 실패 (무시): %s", close_e)
                for retry in range(3):
                    try:
                        await scraper.start()
                        _log.info("브라우저 주기적 재시작 완료")
                        console.print("  [dim]브라우저 재시작 완료[/dim]")
                        break
                    except Exception as restart_e:
                        _log.error("브라우저 재시작 실패 (%d/3): %s", retry + 1, restart_e)
                        if retry < 2:
                            await asyncio.sleep(5)
                        else:
                            console.print(f"  [red]브라우저 재시작 3회 실패: {restart_e}[/red]")
                            console.print("  [red]자동 모드를 종료합니다.[/red]")
                            stop_event.set()
                            break
                if stop_event.is_set():
                    break

            # 강의 목록 새로고침
            try:
                details = await _reload_details(scraper, courses)
            except Exception as e:
                _log.error("강의 목록 갱신 실패: %s", e)
                console.print(f"  [red]강의 목록 갱신 실패: {e}[/red]")

                # 브라우저 연결 끊김 감지 시 자동 재시작
                if "Connection closed" in str(e) or "socket" in str(e).lower():
                    console.print("  [yellow]브라우저 연결 끊김 — 재시작 시도...[/yellow]")
                    _log.info("브라우저 재시작 시도")
                    try:
                        await scraper.close()
                        await scraper.start()
                        console.print("  [dim]브라우저 재시작 완료[/dim]")
                        _log.info("브라우저 재시작 완료")
                    except Exception as restart_e:
                        _log.error("브라우저 재시작 실패: %s", restart_e)
                        console.print(f"  [red]브라우저 재시작 실패: {restart_e}[/red]")
                        # start() 실패 시 부분 생성된 리소스 정리
                        try:
                            await scraper.close()
                        except Exception:
                            pass

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
                e = store.get(url)
                if e:
                    e.played = False
                    e.downloaded = None
                    e.ts = store._now()

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

            # ── 다운로드 누락 점검 (파일시스템 기준 재검증) ─────────
            _check_download_gaps(courses, details, store)

            # STT 모델 메모리 해제 (다음 사이클까지 필요 없음)
            try:
                from src.stt.transcriber import unload_model

                unload_model()
            except Exception as e:
                _log.debug("STT 모델 해제 실패: %s", e)

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
        try:
            from src.stt.transcriber import unload_model

            unload_model()
        except Exception:
            pass

    console.print()
    console.print("  [dim]자동 모드를 종료합니다.[/dim]")
    console.print()


async def _process_lecture(scraper, course, lec, stop_event: asyncio.Event) -> PlayResult:
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
            return PlayResult(played=False, reason="stopped")
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
            last_err_msg = f"재생 실패: {e}"
            _log.error("재생 예외 (%d/%d): %s — %s", play_attempt, _MAX_PLAY_RETRIES, label, e, exc_info=True)
            console.print(f"  [red]  → {last_err_msg} ({play_attempt}/{_MAX_PLAY_RETRIES})[/red]")

    if not play_success:
        _log.warning("재생 최종 실패: %s — %s", label, last_err_msg)
        _tg_error_notify(course, lec, f"{last_err_msg} ({_MAX_PLAY_RETRIES}회 재시도 후 실패)")
        return PlayResult(played=False, reason=last_err_msg or "play_failed")

    lec.completion = "completed"
    _log.info("재생 완료: %s", label)
    console.print("  [dim]  → 재생 완료[/dim]")

    if stop_event.is_set():
        return PlayResult(played=True, downloaded=False, reason="stopped")

    # ── 다운로드 ──────────────────────────────────────────────────
    download_ok, reason, downloadable = await _run_download_step(scraper, course, lec, label)
    if download_ok:
        console.print(f"  [bold green]  → {label} 완료[/bold green]")
        console.print()
    return PlayResult(
        played=True,
        downloaded=download_ok,
        downloadable=downloadable,
        reason=reason,
    )


async def _process_download_only(scraper, course, lec) -> PlayResult:
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

    download_ok, reason, downloadable = await _run_download_step(scraper, course, lec, label)
    return PlayResult(
        played=True,  # 이미 완료된 상태라는 전제
        downloaded=download_ok,
        downloadable=downloadable,
        reason=reason,
    )


async def _run_download_step(scraper, course, lec, label: str) -> tuple[bool, str | None, bool]:
    """run_download를 호출하고 (ok, reason, downloadable) 튜플로 정규화해 반환한다."""
    from src.ui.download import run_download

    rule = Config.DOWNLOAD_RULE or "both"
    audio_only = rule == "audio"
    both = rule == "both"
    _log.info("다운로드 시작: %s", label)
    console.print("  [dim]  → 다운로드 중...[/dim]")
    try:
        result = await run_download(scraper.page, lec, course, audio_only=audio_only, both=both)
    except Exception as e:
        _log.error("다운로드 예외: %s — %s", label, e, exc_info=True)
        console.print(f"  [red]  → 다운로드 실패: {e}[/red]")
        _tg_error_notify(course, lec, f"다운로드 실패: {e}")
        return False, f"exception:{type(e).__name__}", True

    if result.ok:
        _log.info("다운로드 완료: %s", label)
        console.print("  [dim]  → 다운로드 완료[/dim]")
        return True, None, True

    _log.warning("다운로드 실패: %s — reason=%s", label, result.reason)
    console.print(f"  [yellow]  → 다운로드 실패: {label} (사유={result.reason})[/yellow]")
    downloadable = result.reason != REASON_UNSUPPORTED
    return False, result.reason, downloadable


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


def _check_download_gaps(courses, details, store: ProgressStore | None = None) -> list[tuple]:
    """시청 완료된 강의 중 다운로드 파일이 누락된 항목을 점검한다.

    store가 주어지면 파일이 존재하는 항목을 "확정 다운로드 완료"로 기록한다.
    반환값은 누락 튜플 목록 — 호출자가 추가 복구 루프를 돌릴 때 사용.
    """
    from pathlib import Path

    from src.downloader.video_downloader import make_filepath

    download_dir = Config.get_download_dir()
    rule = Config.DOWNLOAD_RULE or "both"
    missing: list[tuple] = []

    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for lec in detail.all_video_lectures:
            if lec.completion != "completed":
                continue
            # 다운로드 불가능한 항목(learningx 등) 제외
            if not lec.is_downloadable:
                if store is not None:
                    store.mark_unsupported(lec.full_url, reason=REASON_UNSUPPORTED)
                continue
            mp4_relpath = make_filepath(course.long_name, lec.week_label, lec.title)
            mp4_path = Path(download_dir) / mp4_relpath
            mp3_path = mp4_path.with_suffix(".mp3")

            has_video = mp4_path.exists()
            has_audio = mp3_path.exists()
            present = (
                (rule == "video" and has_video)
                or (rule == "audio" and has_audio)
                or (rule == "both" and has_video and has_audio)
                or (rule not in {"video", "audio", "both"} and (has_video or has_audio))
            )
            if store is not None and present:
                store.mark_download_confirmed_from_filesystem(lec.full_url)

            if rule == "video" and not has_video:
                missing.append((course.long_name, lec.week_label, lec.title, "mp4"))
            elif rule == "audio" and not has_audio:
                missing.append((course.long_name, lec.week_label, lec.title, "mp3"))
            elif rule == "both" and not (has_video and has_audio):
                missing.append((course.long_name, lec.week_label, lec.title, "mp4+mp3"))

    if missing:
        console.print()
        console.print(f"  [yellow]다운로드 누락 {len(missing)}건 감지:[/yellow]")
        for course_name, week, title, ftype in missing[:10]:
            console.print(f"  [dim]  → [{course_name}] {week} {title} ({ftype})[/dim]")
        if len(missing) > 10:
            console.print(f"  [dim]  → ... 외 {len(missing) - 10}건[/dim]")
        _log.warning("다운로드 누락 %d건 감지 (rule=%s)", len(missing), rule)
        for course_name, week, title, ftype in missing:
            _log.warning("  · [%s] %s %s (누락=%s)", course_name, week, title, ftype)

        # 텔레그램 알림
        creds = Config.get_telegram_credentials()
        if creds:
            from src.notifier.telegram_notifier import notify_download_gaps

            notify_download_gaps(creds[0], creds[1], missing)

    return missing


def _tg_error_notify(course, lec, error_msg: str) -> None:
    """자동 모드 처리 오류를 텔레그램으로 알린다."""
    creds = Config.get_telegram_credentials()
    if not creds:
        return
    try:
        from src.notifier.telegram_notifier import notify_auto_error

        notify_auto_error(creds[0], creds[1], course.long_name, lec.week_label, lec.title, error_msg)
    except Exception as e:
        _log.debug("텔레그램 오류 알림 실패: %s", e)
