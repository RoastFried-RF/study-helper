"""누락 다운로드 복구 TUI.

재생(출석)은 완료됐지만 data/downloads에 파일이 없는 강의를 전수 검사한 뒤
사용자 확인을 받아 순차적으로 다운로드→mp3→STT→요약 파이프라인을 재실행한다.
learningx 타입은 구조적으로 다운로드 불가이므로 자동 제외된다.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from src.config import Config
from src.downloader.result import DownloadResult
from src.downloader.video_downloader import make_filepath
from src.logger import get_logger
from src.ui.download import run_download

console = Console()
_log = get_logger("recover")


def _collect_missing(courses, details, download_dir: str, rule: str) -> list[tuple]:
    missing: list[tuple] = []
    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for lec in detail.all_video_lectures:
            if lec.completion != "completed":
                continue
            if not lec.is_downloadable:
                continue
            mp4_rel = make_filepath(course.long_name, lec.week_label, lec.title)
            mp4 = (Path(download_dir) / mp4_rel).resolve()
            mp3 = mp4.with_suffix(".mp3")
            has_video = mp4.exists()
            has_audio = mp3.exists()
            if rule == "video" and not has_video:
                missing.append((course, lec, "mp4"))
            elif rule == "audio" and not has_audio:
                missing.append((course, lec, "mp3"))
            elif rule == "both" and not (has_video and has_audio):
                parts = []
                if not has_video:
                    parts.append("mp4")
                if not has_audio:
                    parts.append("mp3")
                missing.append((course, lec, "+".join(parts)))
    return missing


async def run_recover(scraper, courses, details) -> None:
    """누락 복구 흐름을 실행한다.

    Args:
        scraper: CourseScraper
        courses: Course 목록
        details: CourseDetail 목록 (courses와 동일 순서)
    """
    download_dir = Config.get_download_dir()
    rule = Config.DOWNLOAD_RULE or "both"

    console.clear()
    console.print()
    console.print("  [bold cyan]누락 다운로드 복구[/bold cyan]")
    console.print()
    console.print(f"  다운로드 규칙: {rule}")
    console.print(f"  다운로드 경로: {download_dir}")
    console.print()

    # 최신 상태로 상세 정보 재로딩 (명령 시점의 LMS completion 상태 기준)
    console.print("  [dim]강의 목록 갱신 중...[/dim]")
    try:
        from src.ui.courses import _reload_details

        details = await _reload_details(scraper, courses)
    except Exception as e:
        _log.error("강의 목록 갱신 실패: %s", e, exc_info=True)
        console.print(f"  [red]강의 목록 갱신 실패: {e}[/red]")
        return

    missing = _collect_missing(courses, details, download_dir, rule)
    _log.info("복구 대상 %d건 수집", len(missing))

    if not missing:
        console.print("  [green]누락된 다운로드가 없습니다.[/green]")
        console.print()
        return

    console.print(f"  [yellow]누락 {len(missing)}건:[/yellow]")
    for course, lec, kind in missing[:20]:
        console.print(f"    [dim]- [{course.long_name}] {lec.week_label} {lec.title} ({kind})[/dim]")
    if len(missing) > 20:
        console.print(f"    [dim]... 외 {len(missing) - 20}건[/dim]")
    console.print()

    answer = Prompt.ask(f"  {len(missing)}건을 복구하시겠습니까?", choices=["y", "n"], default="n")
    if answer != "y":
        console.print("  [dim]취소됨[/dim]")
        return

    # ── 순차 복구 실행 ─────────────────────────────────────
    audio_only = rule == "audio"
    both = rule == "both"
    success = 0
    reasons: Counter = Counter()
    for i, (course, lec, _kind) in enumerate(missing, 1):
        label = f"[{course.long_name}] {lec.title}"
        console.print(f"\n  [{i}/{len(missing)}] {label}")
        _log.info("복구 중 (%d/%d): %s", i, len(missing), label)
        try:
            result: DownloadResult = await run_download(
                scraper.page, lec, course, audio_only=audio_only, both=both
            )
        except Exception as e:
            _log.error("복구 예외: %s — %s", label, e, exc_info=True)
            reasons[f"exception:{type(e).__name__}"] += 1
            console.print(f"    [red]→ 실패 (예외={type(e).__name__})[/red]")
            continue
        if result.ok:
            success += 1
            _log.info("복구 성공: %s", label)
            console.print(f"    [green]→ 성공[/green]")
        else:
            reasons[result.reason] += 1
            _log.warning("복구 실패: %s — reason=%s", label, result.reason)
            console.print(f"    [yellow]→ 실패 (사유={result.reason})[/yellow]")

    console.print()
    console.print(f"  [bold]복구 결과: 성공 {success}/{len(missing)}[/bold]")
    if reasons:
        console.print("  [dim]실패 사유 분포:[/dim]")
        for r, n in reasons.most_common():
            console.print(f"    [dim]{r}: {n}건[/dim]")
    _log.info("복구 종료: 성공 %d/%d, 실패=%s", success, len(missing), dict(reasons))
    console.print()
