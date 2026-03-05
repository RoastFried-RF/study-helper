from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from rich.text import Text
from rich import box

from src.scraper.models import Course, CourseDetail

console = Console()


def show_loading(message: str):
    console.print(f"  [yellow]{message}[/yellow]")


def show_course_list(courses: List[Course], details: List[Optional[CourseDetail]]) -> Optional[Course]:
    """
    과목 목록을 테이블로 표시하고 선택된 Course를 반환한다.
    0 입력 시 None 반환 (종료).
    details는 courses와 같은 순서의 CourseDetail 리스트 (로딩 실패 시 None).
    """
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

    while True:
        choice = Prompt.ask("  과목 선택 [dim](0: 종료)[/dim]")
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(courses):
            return courses[int(choice) - 1]
        console.print("  [red]올바른 번호를 입력하세요.[/red]")
