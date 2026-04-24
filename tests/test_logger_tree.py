"""LOG-SYS-1 회귀 방지 테스트.

프로젝트 전역 로거 팩토리(`src.logger.get_logger`) 가 생성하는 로거는
`study_helper.*` 네임스페이스를 사용해야 한다. `logging.getLogger(__name__)`
패턴을 직접 쓰면 `src.*` 네임스페이스로 빠져 root 핸들러가 없는 상태에서
silent log loss 가 발생한다.

본 테스트는 주요 모듈의 로거 이름이 `study_helper.` 접두사로 시작하는지
assert 하여 회귀를 차단한다.
"""

from __future__ import annotations

import pytest

from src.logger import get_logger


@pytest.fixture(autouse=True, scope="module")
def _bootstrap_logger():
    """get_logger 를 한 번 호출해 study_helper 트리에 파일 핸들러를 부착."""
    get_logger("main")


_EXPECTED_MODULE_LOGGERS: list[tuple[str, str]] = [
    ("src.converter.audio_converter", "_log"),
    ("src.downloader.video_downloader", "_dl_log"),
    ("src.notifier.telegram_notifier", "_log"),
    ("src.stt.transcriber", "_log"),
    ("src.service.progress_store", "_log"),
    ("src.service.download_pipeline", "_log"),
]


@pytest.mark.parametrize(("module_name", "attr"), _EXPECTED_MODULE_LOGGERS)
def test_module_logger_is_study_helper_child(module_name: str, attr: str) -> None:
    import importlib

    mod = importlib.import_module(module_name)
    logger = getattr(mod, attr)
    assert logger.name.startswith("study_helper"), (
        f"{module_name}.{attr}.name = {logger.name!r} — "
        f"study_helper 트리 밖 → silent log loss (LOG-SYS-1 회귀)"
    )


def test_study_helper_root_has_handler() -> None:
    """study_helper 루트 로거에는 TimedRotatingFileHandler 가 붙어 있어야 한다."""
    from logging.handlers import TimedRotatingFileHandler

    logger = get_logger("main")
    parent = logger.parent if logger.name != "study_helper" else logger
    assert parent is not None
    assert any(isinstance(h, TimedRotatingFileHandler) for h in parent.handlers), (
        f"study_helper 로거에 파일 핸들러 없음 — 로그 파일 영속성 상실. handlers={parent.handlers}"
    )
