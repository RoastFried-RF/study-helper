"""
로그 모듈.

## API 선택 규약 (LOG-SYS-4)

- **신규 코드는 `get_logger(이름)` 만 사용**하라. 앱 전역 로거 트리에 귀속되어
  `logs/study_helper.log` 에 일별 로테이션 + 7일 보관으로 기록된다.
- `get_error_logger("download"/"play")` 은 **기존 호환 경로** (deprecated).
  특정 동작의 실패 컨텍스트를 독립 타임스탬프 파일로 남기는 기존 동작 유지용.
  신규 호출은 추가하지 말 것. 기존 호출도 점진적으로 `get_logger` 로 이관 권장.

## 보존 정책

- `study_helper.log`: `TimedRotatingFileHandler(when="midnight", backupCount=7)` — 7일
- `YYYYMMDD_HHMMSS_<action>.log`: LOG-SYS-2 에 따라 14일 초과 시 자동 삭제

## 보안

- LOG-SYS-3: 모든 로거에 `SensitiveFilter` 가 자동 부착되어 PII/OAuth/봇 토큰을 마스킹.
- SEC-008: 새로 생성되는 로그 파일은 POSIX 에서 0o600 권한.
"""

import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.util.log_sanitize import mask_sensitive


class SensitiveFilter(logging.Filter):
    """LOG-SYS-3: 모든 log record 메시지에 PII/OAuth 마스킹 적용.

    `record.msg` 와 `record.args` 에 있는 문자열을 mask_sensitive 로 치환한다.
    player 등에서 명시적으로 이미 마스킹된 값에 대해서도 멱등(재적용 무해).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = mask_sensitive(record.msg)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        mask_sensitive(a) if isinstance(a, str) else a for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (mask_sensitive(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
        except Exception:
            # 로그 필터 자체의 실패가 앱을 중단시키면 안 된다.
            pass
        return True


_SENSITIVE_FILTER = SensitiveFilter()

# KST (UTC+9) — src.config.KST 와 동일 정의.
# Docker 컨테이너에서 TZ 미설정 시에도 일관된 날짜로 로그 파일명을 생성하기 위해
# logger 내부에서도 aware datetime 을 사용한다 (circular import 회피용 inline 정의).
_KST = timezone(timedelta(hours=9))


def _logs_dir() -> Path:
    """로그 디렉토리를 반환한다 (ARCH-009).

    config.get_logs_path() 단일 소스에서 해결하되, config 가 crypto 를 import
    하고 crypto 도 logging 을 쓸 수 있어 top-level import 는 circular 유발.
    함수 호출 시점에 import.
    """
    from src.config import get_logs_path

    return get_logs_path()

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

        logs_dir = _logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "study_helper.log"

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
        _app_logger.addFilter(_SENSITIVE_FILTER)  # LOG-SYS-3: 전역 마스킹
        _app_logger.addHandler(file_handler)
        # SEC-008: 로그 파일 권한 0o600 (POSIX). Windows 는 no-op.
        try:
            log_path.chmod(0o600)
        except OSError:
            pass
        _app_logger.info("로그 디렉토리: %s", logs_dir.resolve())

    if name == "study_helper":
        return _app_logger
    return _app_logger.getChild(name.removeprefix("study_helper."))


_error_loggers: dict[str, tuple[logging.Logger, Path]] = {}
_error_retention_cleaned: bool = False

# LOG-SYS-2: get_error_logger 가 만드는 YYYYMMDD_HHMMSS_<action>.log 보존 기간 (일).
# TimedRotatingFileHandler 의 backupCount=7 은 study_helper.log 만 해당하므로
# 이 정책이 없으면 에러 전용 로그가 무한 누적된다.
_ERROR_LOG_RETENTION_DAYS = 14


def _cleanup_old_error_logs(logs_dir: Path) -> None:
    """14일 초과 에러 로그 파일을 삭제한다 (LOG-SYS-2).

    `study_helper.log*` 는 대상 외. `YYYYMMDD_HHMMSS_<action>.log` 파일 이름의
    앞 8자리(YYYYMMDD)로 날짜를 판정한다. 파싱 실패 시 스킵.
    프로세스 당 1회만 수행(캐시 플래그).
    """
    global _error_retention_cleaned
    if _error_retention_cleaned:
        return
    _error_retention_cleaned = True

    cutoff_date = datetime.now(_KST).date()
    cutoff_ordinal = cutoff_date.toordinal() - _ERROR_LOG_RETENTION_DAYS

    try:
        files = list(logs_dir.glob("*_*.log"))
    except OSError:
        return

    for path in files:
        if path.name.startswith("study_helper"):
            continue
        stem = path.stem
        if len(stem) < 8 or not stem[:8].isdigit():
            continue
        try:
            file_date = datetime.strptime(stem[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if file_date.toordinal() < cutoff_ordinal:
            try:
                path.unlink()
            except OSError:
                pass


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
    오류 기록용 파일 로거를 생성하거나 재사용한다.

    .. deprecated:: LOG-SYS-4
        신규 코드는 `get_logger(이름)` 을 사용하라. 본 함수는 기존 "download" /
        "play" 호출 사이트 호환 유지용이다. 독립 타임스탬프 파일로 에러 컨텍스트를
        남기는 기존 동작은 유지되며, 14일 초과 시 자동 정리된다 (LOG-SYS-2).

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
    today = datetime.now(_KST).strftime("%Y%m%d")
    cache_key = f"{action}_{today}"

    if cache_key in _error_loggers:
        return _error_loggers[cache_key]

    # 날짜가 변경되었으면 이전 핸들러 정리
    _cleanup_stale_error_loggers(today)

    logs_dir = _logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_error_logs(logs_dir)

    timestamp = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"{timestamp}_{action}.log"

    logger = logging.getLogger(f"study_helper.error.{cache_key}")
    if not logger.hasHandlers():
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        # LOG-SYS-5: 전역 로거와 동일 포맷 — grep 시 필드 위치 일관성 유지.
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addFilter(_SENSITIVE_FILTER)  # LOG-SYS-3
        logger.addHandler(handler)
        # SEC-008: 에러 로그 파일도 0o600 (POSIX). Windows 는 no-op.
        try:
            log_path.chmod(0o600)
        except OSError:
            pass

    _error_loggers[cache_key] = (logger, log_path)
    return logger, log_path
