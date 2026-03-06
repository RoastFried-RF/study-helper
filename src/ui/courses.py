from enum import Enum
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from rich.text import Text
from rich import box

from src.scraper.models import Course, CourseDetail, LectureItem, Week

console = Console()


class LectureAction(Enum):
    PLAY = "play"
    DOWNLOAD_VIDEO = "download_video"
    DOWNLOAD_AUDIO = "download_audio"
    CANCEL = "cancel"


def show_loading(message: str):
    console.print(f"  [yellow]{message}[/yellow]")


def _redraw_course_list(courses: List[Course], details: List[Optional[CourseDetail]]) -> None:
    """과목 목록 테이블을 (재)출력한다."""
    console.clear()
    console.print(Panel(
        Text("수강 중인 과목 목록", justify="center", style="bold cyan"),
        border_style="cyan",
        padding=(0, 4),
    ))
    console.print()

    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
        expand=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("과목명", min_width=20)
    table.add_column("미시청 / 전체", justify="center", width=14)
    table.add_column("학기", width=12, style="dim")

    for i, (course, detail) in enumerate(zip(courses, details), start=1):
        if detail is not None:
            pending = detail.pending_video_count
            total = detail.total_video_count
            if pending == 0:
                watch_str = Text(f"{pending} / {total}", style="green")
            else:
                watch_str = Text(f"{pending} / {total}", style="yellow bold")
        else:
            watch_str = Text("- / -", style="dim")

        table.add_row(
            str(i),
            course.long_name,
            watch_str,
            course.term,
        )

    console.print(table)
    console.print()


def show_course_list(courses: List[Course], details: List[Optional[CourseDetail]]) -> Optional[Course]:
    """
    과목 목록을 테이블로 표시하고 선택된 Course를 반환한다.
    0 입력 시 None 반환 (종료). 'setting' 입력 시 설정 화면으로 이동.
    details는 courses와 같은 순서의 CourseDetail 리스트 (로딩 실패 시 None).
    """
    _redraw_course_list(courses, details)

    while True:
        choice = Prompt.ask("  과목 선택 [dim](0: 종료 / setting: 설정)[/dim]")
        if choice == "0":
            return None
        if choice.lower() == "setting":
            from src.ui.settings import run_settings
            run_settings()
            _redraw_course_list(courses, details)
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(courses):
            return courses[int(choice) - 1]
        console.print("  [red]올바른 번호를 입력하세요.[/red]")


def show_week_list(course: Course, detail: CourseDetail) -> Optional[tuple[LectureItem, LectureAction]]:
    """
    선택한 과목의 주차별 강의 목록을 표시하고 강의를 선택할 수 있다.
    강의 선택 후 액션(재생/다운로드/취소)을 선택하면 (LectureItem, LectureAction) 반환.
    과목 목록으로 돌아가면 None 반환.
    """
    while True:
        all_lectures = _render_week_list(course, detail)
        if not all_lectures:
            return None

        # 강의 번호 선택
        while True:
            choice = Prompt.ask("  강의 선택 [dim](0: 돌아가기)[/dim]")
            if choice == "0":
                return None
            if choice.isdigit() and 1 <= int(choice) <= len(all_lectures):
                selected_lec = all_lectures[int(choice) - 1]
                break
            console.print("  [red]올바른 번호를 입력하세요.[/red]")

        # 액션 메뉴
        action = _show_lecture_action_menu(selected_lec)
        if action != LectureAction.CANCEL:
            return selected_lec, action
        # 취소 시 강의 목록으로 돌아감


def _render_week_list(course: Course, detail: CourseDetail) -> list[LectureItem]:
    """주차별 강의 목록을 출력하고 전체 영상 강의 리스트를 반환한다."""
    console.clear()
    console.print(Panel(
        Text(course.long_name, justify="center", style="bold cyan"),
        border_style="cyan",
        padding=(0, 4),
    ))
    console.print()

    video_weeks = [w for w in detail.weeks if w.video_lectures]
    if not video_weeks:
        console.print("  [dim]영상 강의가 없습니다.[/dim]")
        console.print()
        Prompt.ask("  [dim]Enter를 눌러 돌아가기[/dim]", default="")
        return []

    all_lectures: list[LectureItem] = []

    for week in video_weeks:
        pending = week.pending_count
        total = len(week.video_lectures)

        if pending == 0:
            count_text = Text(f"  {pending} / {total}", style="green")
        else:
            count_text = Text(f"  {pending} / {total}", style="yellow bold")

        header = Text()
        header.append(f"  {week.title}", style="bold")
        header.append("  ")
        header.append("미시청 / 전체: ", style="dim")
        header.append(count_text)

        console.print(header)

        table = Table(
            box=box.SIMPLE,
            show_header=False,
            padding=(0, 2),
            expand=False,
        )
        table.add_column("#", width=4, justify="right", style="dim")
        table.add_column("완료", width=4, justify="center")
        table.add_column("강의명", min_width=30)
        table.add_column("기간", style="dim")
        table.add_column("길이", width=8, justify="right", style="dim")

        for lec in week.video_lectures:
            all_lectures.append(lec)
            num = str(len(all_lectures))

            if lec.is_upcoming:
                done_mark = Text("예정", style="dim")
            elif lec.completion == "completed":
                done_mark = Text("✓", style="green")
            else:
                done_mark = Text("○", style="yellow")

            if lec.start_date and lec.end_date:
                date_str = f"{lec.start_date} ~ {lec.end_date}"
            elif lec.start_date:
                date_str = f"{lec.start_date} ~"
            else:
                date_str = ""

            duration = lec.duration or "-"
            table.add_row(num, done_mark, lec.title, date_str, duration)

        console.print(table)

    console.print()
    return all_lectures


def _show_lecture_action_menu(lec: LectureItem) -> LectureAction:
    """선택한 강의에 대한 액션 메뉴를 표시하고 선택된 액션을 반환한다."""
    console.print()
    console.print(Panel(
        Text(lec.title, justify="center", style="bold"),
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()
    console.print("  [bold]1.[/bold] 재생  [dim](백그라운드 출석 처리)[/dim]")
    console.print("  [bold]2.[/bold] 영상 다운로드  [dim](mp4)[/dim]")
    console.print("  [bold]3.[/bold] 음성 다운로드  [dim](mp3)[/dim]")
    console.print("  [bold]4.[/bold] 취소")
    console.print()

    while True:
        choice = Prompt.ask("  선택", choices=["1", "2", "3", "4"], show_choices=False)
        if choice == "1":
            return LectureAction.PLAY
        if choice == "2":
            return LectureAction.DOWNLOAD_VIDEO
        if choice == "3":
            return LectureAction.DOWNLOAD_AUDIO
        return LectureAction.CANCEL
