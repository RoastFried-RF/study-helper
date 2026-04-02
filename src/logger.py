"""
로그 모듈.

1. 앱 전역 로거 — 세션 단위 로그 파일 + 콘솔 출력
2. 에러 전용 로거 — 개별 동작(play, download)별 에러 로그 (기존 호환)

로그 파일: logs/study_helper_YYYYMMDD.log (일별 로테이션, 7일 보관)
에러 파일: logs/YYYYMMDD_HHMMSS_<action>.log (기존 동작 유지)
"""

import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LOGS_DIR = Path(__file__).parent.parent / "logs"

# Docker 환경에서는 /data/logs 사용
if Path("/data").exists():
    _LOGS_DIR = Path("/data/logs")
elif os.getenv("STUDY_HELPER_DATA_DIR"):
    _LOGS_DIR = Path(os.getenv("STUDY_HELPER_DATA_DIR")) / "logs"

_app_logger: logging.Logger | None = None


def get_logger(name: str = "study_helper") -> logging.Logger:
    """앱 전역 로거를 반환한다.

    최초 호출 시 파일 핸들러(일별 로테이션)를 설정한다.
    이후 호출에서는 child 로거를 반환한다.
    """
    global _app_logger

    if _app_logger is None:
        _app_logger = logging.getLogger("study_helper")
        _app_logger.setLevel(logging.DEBUG)
        _app_logger.propagate = False

        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOGS_DIR / "study_helper.log"

        # 일별 로테이션, 7일 보관
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        _app_logger.addHandler(file_handler)

    if name == "study_helper":
        return _app_logger
    return _app_logger.getChild(name.removeprefix("study_helper."))


_error_loggers: dict[str, tuple[logging.Logger, Path]] = {}


def _cleanup_stale_error_loggers(today: str) -> None:
    """오늘이 아닌 날짜의 에러 로거 핸들러를 닫고 캐시에서 제거한다."""
    stale_keys = [k for k in _error_loggers if not k.endswith(today)]
    for key in stale_keys:
        logger, _ = _error_loggers.pop(key)
        for handler in logger.handlers[:]:
            try:
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)


def get_error_logger(action: str) -> tuple[logging.Logger, Path]:
    """
    오류 기록용 파일 로거를 생성하거나 재사용한다. (기존 호환)

    같은 action + 같은 날짜의 호출은 기존 로거를 재사용하여
    핸들러/파일 디스크립터 누적을 방지한다.
    날짜가 변경되면 이전 날짜의 핸들러를 자동으로 닫는다.

    Args:
        action: 로그 파일 이름에 포함할 동작 식별자 (예: "play", "download")

    Returns:
        (logger, log_path) — 로거와 로그 파일 경로
    """
    # 경로 탐색 방지
    action = action.replace("/", "_").replace("\\", "_").replace("..", "_")
    today = datetime.now().strftime("%Y%m%d")
    cache_key = f"{action}_{today}"

    if cache_key in _error_loggers:
        return _error_loggers[cache_key]

    # 날짜가 변경되었으면 이전 핸들러 정리
    _cleanup_stale_error_loggers(today)

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _LOGS_DIR / f"{timestamp}_{action}.log"

    logger = logging.getLogger(f"study_helper.error.{cache_key}")
    if not logger.hasHandlers():
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    _error_loggers[cache_key] = (logger, log_path)
    return logger, log_path
