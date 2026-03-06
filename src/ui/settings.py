"""
설정 UI.

최초 실행 시 또는 'setting' 명령으로 진입하는 설정 화면.
다운로드 경로, 다운로드 규칙, STT, AI 요약 항목을 순서대로 질의한다.
"""

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from src.config import Config, _default_download_dir
from src.summarizer.summarizer import GEMINI_MODEL_IDS, GEMINI_MODEL_LABELS, GEMINI_DEFAULT_MODEL

console = Console()

_WHISPER_MODELS = ["tiny", "base", "small", "medium", "large"]


def run_settings() -> None:
    """
    설정 화면을 표시하고 결과를 Config / .env에 저장한다.
    최초 실행 또는 'setting' 입력 시 호출된다.
    """
    console.clear()
    console.print(Panel(
        Text("설정", justify="center", style="bold cyan"),
        border_style="cyan",
        padding=(0, 4),
    ))
    console.print()
    console.print("  [dim]Enter를 누르면 현재 값(또는 기본값)을 유지합니다.[/dim]")
    console.print()

    # ── 1. 다운로드 경로 ─────────────────────────────────────────
    _print_section("1. 다운로드 경로")
    default_dir = Config.get_download_dir()
    console.print(f"  [dim]현재값: {default_dir}[/dim]")
    console.print()

    while True:
        raw = Prompt.ask("  경로 입력", default="").strip()
        download_dir = raw if raw else default_dir
        path = Path(download_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            break
        except Exception as e:
            console.print(f"  [red]경로를 생성할 수 없습니다: {e}[/red]")
            console.print("  [dim]다시 입력해주세요.[/dim]")
    console.print()

    # ── 2. 다운로드 규칙 ─────────────────────────────────────────
    _print_section("2. 다운로드 규칙")
    _current = {"video": "영상만 (mp4)", "audio": "음성만 (mp3)", "both": "영상 + 음성"}.get(
        Config.DOWNLOAD_RULE, "미설정"
    )
    console.print(f"  [dim]현재값: {_current}[/dim]")
    console.print()
    console.print("  [bold]1.[/bold] 영상만  [dim](mp4)[/dim]")
    console.print("  [bold]2.[/bold] 음성만  [dim](mp3)[/dim]")
    console.print("  [bold]3.[/bold] 영상 + 음성  [dim](mp4 + mp3)[/dim]")
    console.print()

    _rule_default = {"video": "1", "audio": "2", "both": "3"}.get(Config.DOWNLOAD_RULE, "1")
    rule_choice = Prompt.ask("  선택", choices=["1", "2", "3"], default=_rule_default, show_choices=False)
    download_rule = {"1": "video", "2": "audio", "3": "both"}[rule_choice]
    console.print()

    # ── 2.1. STT (텍스트 변환) — 영상만이 아닌 경우만 ─────────────
    stt_enabled = False
    if download_rule != "video":
        _print_section("2.1. 텍스트 변환 (STT)")
        console.print("  [dim]음성에서 텍스트를 추출합니다 (Whisper 로컬 실행).[/dim]")
        _stt_default = "y" if Config.STT_ENABLED == "true" else "n"
        stt_choice = Prompt.ask("  STT 사용", choices=["y", "n"], default=_stt_default, show_choices=True)
        stt_enabled = stt_choice == "y"

        if stt_enabled:
            _print_section("  Whisper 모델 크기")
            console.print("  [dim]작을수록 빠르지만 정확도 낮음 (기본: base)[/dim]")
            for i, m in enumerate(_WHISPER_MODELS, 1):
                console.print(f"  [bold]{i}.[/bold] {m}")
            console.print()
            _model_default = str(_WHISPER_MODELS.index(Config.WHISPER_MODEL) + 1) if Config.WHISPER_MODEL in _WHISPER_MODELS else "2"
            model_choice = Prompt.ask("  모델 선택", choices=[str(i) for i in range(1, 6)], default=_model_default, show_choices=False)
            Config.WHISPER_MODEL = _WHISPER_MODELS[int(model_choice) - 1]
            Config._save_env({"WHISPER_MODEL": Config.WHISPER_MODEL})
        console.print()

    # ── 3. AI 요약 ───────────────────────────────────────────────
    _print_section("3. AI 요약")
    console.print("  [dim]STT로 변환된 텍스트를 AI로 자동 요약합니다.[/dim]")
    console.print("  [dim]현재 Gemini API를 지원합니다 (무료 티어 사용 가능).[/dim]")
    console.print()
    _ai_default = "y" if Config.AI_ENABLED == "true" else "n"
    ai_choice = Prompt.ask("  AI 요약 사용", choices=["y", "n"], default=_ai_default, show_choices=True)
    ai_enabled = ai_choice == "y"
    console.print()

    ai_agent = "gemini"
    api_key = ""
    gemini_model = Config.GEMINI_MODEL or GEMINI_DEFAULT_MODEL

    if ai_enabled:
        # 3.1. Gemini API 키
        _print_section("3.1. Gemini API 키 입력")
        console.print("  [dim]Google AI Studio에서 무료로 발급 가능합니다.[/dim]")
        _existing_key = Config.GOOGLE_API_KEY
        if _existing_key:
            console.print(f"  [dim]현재 키: {_existing_key[:8]}{'*' * 20}[/dim]")
            console.print("  [dim]변경하지 않으려면 Enter를 누르세요.[/dim]")
        console.print()

        raw_key = Prompt.ask("  API 키", default="", password=True).strip()
        api_key = raw_key if raw_key else _existing_key
        console.print()

        # API 키가 있을 때만 모델 선택
        if api_key:
            # 3.2. Gemini 모델 선택
            _print_section("3.2. Gemini 모델 선택")
            console.print("  [dim]무료 티어 모델 사용 권장 (기본값: gemini-2.5-flash)[/dim]")
            console.print()
            for i, label in enumerate(GEMINI_MODEL_LABELS, 1):
                console.print(f"  [bold]{i}.[/bold] {label}")
            console.print()

            _current_model = Config.GEMINI_MODEL or GEMINI_DEFAULT_MODEL
            _model_default = str(GEMINI_MODEL_IDS.index(_current_model) + 1) if _current_model in GEMINI_MODEL_IDS else "1"
            model_choice = Prompt.ask(
                "  모델 선택",
                choices=[str(i) for i in range(1, len(GEMINI_MODEL_IDS) + 1)],
                default=_model_default,
                show_choices=False,
            )
            gemini_model = GEMINI_MODEL_IDS[int(model_choice) - 1]
            console.print()

    # ── 저장 ────────────────────────────────────────────────────
    Config.save_settings(
        download_dir=str(Path(download_dir).resolve()),
        download_rule=download_rule,
        stt_enabled=stt_enabled,
        ai_enabled=ai_enabled,
        ai_agent=ai_agent,
        api_key=api_key,
        gemini_model=gemini_model,
    )

    console.print("  [bold green]설정이 저장되었습니다.[/bold green]")
    console.print()
    _print_summary(download_dir, download_rule, stt_enabled, ai_enabled, gemini_model if ai_enabled and api_key else "")
    console.print()
    Prompt.ask("  [dim]Enter를 눌러 계속[/dim]", default="")


def _print_section(title: str) -> None:
    console.print(f"  [bold]{title}[/bold]")
    console.print()


def _print_summary(download_dir: str, download_rule: str,
                   stt_enabled: bool, ai_enabled: bool, gemini_model: str) -> None:
    """설정 요약을 표시한다."""
    rule_label = {"video": "영상만 (mp4)", "audio": "음성만 (mp3)", "both": "영상 + 음성"}.get(download_rule, download_rule)
    console.print("  [dim]─────────────────────────────[/dim]")
    console.print(f"  다운로드 경로  : [cyan]{download_dir}[/cyan]")
    console.print(f"  다운로드 규칙  : [cyan]{rule_label}[/cyan]")
    if download_rule != "video":
        console.print(f"  STT 변환      : [cyan]{'사용' if stt_enabled else '미사용'}[/cyan]")
    console.print(f"  AI 요약       : [cyan]{'사용' if ai_enabled else '미사용'}[/cyan]")
    if ai_enabled and gemini_model:
        console.print(f"  Gemini 모델   : [cyan]{gemini_model}[/cyan]")
    console.print("  [dim]─────────────────────────────[/dim]")
