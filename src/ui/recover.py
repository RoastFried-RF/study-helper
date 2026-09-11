"""누락 다운로드 복구 TUI.

수집/실행 로직은 src/service/recover_pipeline.py가 단일 소스로 제공하고,
이 모듈은 Rich 콘솔 출력과 사용자 확인 프롬프트만 담당한다.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt

from src.config import Config
from src.downloader.result import DownloadResult
from src.logger import get_logger
from src.service.recover_pipeline import MissingItem, collect_missing, run_recovery

console = Console()
_log = get_logger("recover")


async def run_recover(scraper, courses, details) -> None:
    """누락 복구 TUI 흐름.

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

    missing = collect_missing(courses, details)
    _log.info("복구 대상 %d건 수집", len(missing))

    if not missing:
        console.print("  [green]누락된 다운로드가 없습니다.[/green]")
        console.print()
        return

    console.print(f"  [yellow]누락 {len(missing)}건:[/yellow]")
    for item in missing[:20]:
        console.print(
            f"    [dim]- [{item.course.long_name}] {item.lec.week_label} {item.lec.title} ({item.kind})[/dim]"
        )
    if len(missing) > 20:
        console.print(f"    [dim]... 외 {len(missing) - 20}건[/dim]")
    console.print()

    answer = Prompt.ask(
        f"  {len(missing)}건을 복구하시겠습니까?",
        choices=["y", "n"],
        default="n",
    )
    if answer != "y":
        console.print("  [dim]취소됨[/dim]")
        return

    # ── 순차 복구 실행 ─────────────────────────────────────
    def _on_progress(index: int, total: int, item: MissingItem, result: DownloadResult | Exception | None) -> None:
        label = f"[{item.course.long_name}] {item.lec.title}"
        if result is None:
            console.print(f"\n  [{index}/{total}] {label}")
            return
        if isinstance(result, Exception):
            console.print(f"    [red]→ 실패 (예외={type(result).__name__})[/red]")
            return
        if result.ok:
            console.print("    [green]→ 성공[/green]")
        else:
            console.print(f"    [yellow]→ 실패 (사유={result.reason})[/yellow]")

    report = await run_recovery(scraper, missing, on_progress=_on_progress)

    console.print()
    console.print(f"  [bold]복구 결과: 성공 {report.success}/{report.total}[/bold]")
    if report.failed_by_reason:
        console.print("  [dim]실패 사유 분포:[/dim]")
        for r, n in report.failed_by_reason.most_common():
            console.print(f"    [dim]{r}: {n}건[/dim]")
    console.print()
