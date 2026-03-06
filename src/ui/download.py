"""
다운로드 관련 UI.

다운로드 경로 설정 및 확인 화면을 제공한다.
"""

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from src.config import Config, _default_download_dir

console = Console()


def ask_download_dir() -> str:
    """
    다운로드 경로를 사용자에게 묻고, .env에 저장한 뒤 경로를 반환한다.

    - 저장된 경로가 있으면 바로 반환 (묻지 않음)
    - 없으면 기본 경로를 안내하고 Enter 또는 직접 입력 받음
    """
    if Config.has_download_dir():
        return Config.get_download_dir()

    default_dir = _default_download_dir()

    console.print()
    console.print(Panel(
        Text("다운로드 경로 설정", justify="center", style="bold"),
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()
    console.print(f"  [dim]기본 경로는 다운로드 폴더에 저장됩니다:[/dim]")
    console.print(f"  [cyan]{default_dir}[/cyan]")
    console.print()
    console.print("  [dim]다른 경로를 원하면 입력하고, 기본값을 사용하려면 Enter를 누르세요.[/dim]")
    console.print()

    while True:
        user_input = Prompt.ask("  다운로드 경로", default="").strip()

        save_dir = user_input if user_input else default_dir

        # 경로 유효성 검사
        path = Path(save_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            console.print(f"  [red]경로를 생성할 수 없습니다: {e}[/red]")
            console.print("  [dim]다시 입력해주세요.[/dim]")
            continue

        Config.save_download_dir(str(path.resolve()))
        console.print(f"  [green]저장되었습니다:[/green] {Config.DOWNLOAD_DIR}")
        console.print()
        return Config.DOWNLOAD_DIR
