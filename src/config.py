"""환경변수 기반 앱 설정 컨테이너.

.env 파일을 로드한 뒤 `Config` 클래스의 클래스 변수로 노출한다.
민감 값(비밀번호, API 키, 봇 토큰) 은 `enc:` 접두사로 `.env` 에 암호화 저장되며
접근 시점에 자동 복호화된다. 경로 해결 유틸(`get_data_base` / `get_data_path` /
`get_logs_path`) 과 재시도 정책(`RetryPolicy`) 도 본 모듈에서 제공한다.
"""

import os
import sys
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.crypto import decrypt, encrypt, is_encrypted

# ── 공용 상수 ─────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

# .env 파일 경로: STUDY_HELPER_DATA_DIR이 설정되면 그 안의 .env를 사용
_data_dir_env = os.getenv("STUDY_HELPER_DATA_DIR", "")
_env_path = Path(_data_dir_env) / ".env" if _data_dir_env else Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


def _load_credential(env_key: str) -> str:
    """
    환경변수를 읽어 복호화한다.
    복호화 실패 시(키 불일치 등) 빈 문자열을 반환하되, .env 값은 보존한다.
    """
    raw = os.getenv(env_key, "")
    if not raw:
        return ""
    if is_encrypted(raw):
        return decrypt(raw)
    return raw


def _default_download_dir() -> str:
    """OS별 기본 다운로드 경로를 반환한다."""
    if sys.platform == "win32":
        return str(Path.home() / "Downloads")
    # Docker 컨테이너 환경: /data 볼륨이 마운트된 경우 사용
    if Path("/data").exists() and str(Path.home()) == "/root":
        return "/data/downloads"
    # macOS / 일반 Linux
    return str(Path.home() / "Downloads")


def _read_version() -> str:
    """CHANGELOG.md의 첫 번째 ## [vX.Y.Z] 항목에서 버전을 읽어온다."""
    import re

    changelog = Path(__file__).parent.parent / "CHANGELOG.md"
    try:
        with open(changelog, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^## \[v(.+?)\]", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


APP_VERSION = _read_version()


class RetryPolicy:
    """시스템 전역 재시도 정책 (ARCH-010).

    각 모듈에 산재하던 _MAX_*_RETRIES / _RETRY_WAIT / _RETRY_BASE_DELAY 를
    단일 지점으로 수렴. 튜닝 시 여기만 수정한다.
    """

    PLAY = 3  # ui/auto 재생 재시도
    DOWNLOAD = 3  # ui/auto 다운로드 재시도
    URL_EXTRACT = 3  # ui/download URL 추출 재시도
    STREAM = 3  # downloader/video_downloader HTTP 스트리밍 재시도
    TELEGRAM = 3  # notifier/telegram HTTP 호출 재시도

    URL_RETRY_WAIT_SEC = 10  # URL 추출 실패 후 대기 시간
    TELEGRAM_BASE_DELAY = 1.0  # Telegram backoff 기본 (2**attempt 곱)
    BROWSER_RESTART_INTERVAL = 3  # 자동 모드 N 사이클마다 브라우저 재시작

    # BUG-5: 누적 재생 실패 임계 — 재시도 횟수가 아닌 "사이클 누적 실패" 임계.
    # 의미적으로 PLAY (사이클당 attempt) 와 분리되며, 임계 초과 시 영구 격리.
    # 보수적으로 잡아 일시적 driver crash 등 false-positive 격리를 방지.
    PLAY_FAIL_QUARANTINE = 5


def _is_docker_with_data_volume() -> bool:
    """Docker 컨테이너 내부이면서 /data 볼륨이 마운트된 경우에만 True.

    Windows/macOS에서 우연히 드라이브 루트의 \\data 폴더(예: D:\\data)가 있어도
    `Path("/data")`가 해당 경로를 가리켜 오탐하는 문제를 방지하기 위해,
    Linux 플랫폼 + `/.dockerenv` 파일 존재 + `/data` 디렉토리 존재를 모두 검증한다.
    """
    if sys.platform != "linux":
        return False
    return Path("/.dockerenv").exists() and Path("/data").is_dir()


def get_data_base() -> Path:
    """데이터 디렉토리의 base 경로를 반환한다 (ARCH-009).

    우선순위:
    1. STUDY_HELPER_DATA_DIR 환경변수 (Electron 앱이 설정)
    2. Docker 컨테이너(/data 마운트): /data
    3. 로컬: 프로젝트 루트/data (CWD 무관)

    이 함수는 logger.py / crypto.py 등 여러 모듈에서 동일 로직을 복제하던 것을
    수렴한다. 디렉토리는 필요 시 호출자가 mkdir 한다.
    """
    env_dir = os.getenv("STUDY_HELPER_DATA_DIR", "")
    if env_dir:
        return Path(env_dir)
    if _is_docker_with_data_volume():
        return Path("/data")
    return Path(__file__).resolve().parent.parent / "data"


def get_data_path(filename: str) -> Path:
    """데이터 파일의 절대 경로를 반환한다. base 디렉토리는 자동 생성."""
    base = get_data_base()
    base.mkdir(parents=True, exist_ok=True)
    return base / filename


def get_logs_path() -> Path:
    """로그 디렉토리 경로를 반환한다 (ARCH-009).

    STUDY_HELPER_DATA_DIR 하위 `logs/` 를 사용. logger.py 가 직접 env 파싱하던
    중복을 제거한다.
    """
    return get_data_base() / "logs"


class Config:
    """앱 전역 설정 컨테이너. 모든 필드는 클래스 변수로 공개되며
    `.env` 로부터 모듈 import 시점에 로드된다. 민감 필드는 Fernet 으로 암호화 저장."""

    LMS_USER_ID: str = _load_credential("LMS_USER_ID")
    LMS_PASSWORD: str = _load_credential("LMS_PASSWORD")
    GOOGLE_API_KEY: str = _load_credential("GOOGLE_API_KEY")
    OPENAI_API_KEY: str = _load_credential("OPENAI_API_KEY")
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    # STT 언어: ko, en, 빈 문자열(자동 감지)
    STT_LANGUAGE: str = os.getenv("STT_LANGUAGE", "ko")
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "")
    # 다운로드 규칙: video / audio / both
    DOWNLOAD_RULE: str = os.getenv("DOWNLOAD_RULE", "")
    # STT 사용 여부: true / false
    STT_ENABLED: str = os.getenv("STT_ENABLED", "")
    # AI 요약 사용 여부: true / false
    AI_ENABLED: str = os.getenv("AI_ENABLED", "")
    # AI 에이전트 종류: gemini / openai
    AI_AGENT: str = os.getenv("AI_AGENT", "")
    # Gemini 모델 ID
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "")
    # 요약 프롬프트 추가 지시사항
    SUMMARY_PROMPT_EXTRA: str = os.getenv("SUMMARY_PROMPT_EXTRA", "")
    # 텔레그램 봇 연동
    TELEGRAM_ENABLED: str = os.getenv("TELEGRAM_ENABLED", "")
    TELEGRAM_BOT_TOKEN: str = _load_credential("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    # 텔레그램 전송 후 파일 자동 삭제
    TELEGRAM_AUTO_DELETE: str = os.getenv("TELEGRAM_AUTO_DELETE", "")
    # 다운로드 SSRF 허용 도메인 suffix 추가 (쉼표 구분, 예: ".cdn.example.com")
    DOWNLOAD_EXTRA_HOSTS: str = os.getenv("DOWNLOAD_EXTRA_HOSTS", "")

    @classmethod
    def get_telegram_credentials(cls) -> tuple[str, str] | None:
        """텔레그램이 활성화되고 credential이 유효하면 (token, chat_id) 반환."""
        if cls.TELEGRAM_ENABLED != "true":
            return None
        if not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID:
            return None
        return cls.TELEGRAM_BOT_TOKEN, cls.TELEGRAM_CHAT_ID

    @classmethod
    def has_credentials(cls) -> bool:
        return bool(cls.LMS_USER_ID and cls.LMS_PASSWORD)

    @classmethod
    def get_ai_api_key(cls) -> str:
        """현재 AI_AGENT 에 해당하는 API 키를 반환 (ARCH-008)."""
        if cls.AI_AGENT == "gemini":
            return cls.GOOGLE_API_KEY
        if cls.AI_AGENT == "openai":
            return cls.OPENAI_API_KEY
        return ""

    @classmethod
    def get_ai_model(cls) -> str:
        """현재 AI_AGENT 에 해당하는 모델 이름을 반환."""
        if cls.AI_AGENT == "gemini":
            return cls.GEMINI_MODEL
        return ""

    @classmethod
    def has_settings(cls) -> bool:
        """최초 설정이 완료됐는지 확인 (다운로드 규칙 기준)."""
        return bool(cls.DOWNLOAD_RULE)

    @classmethod
    def get_download_dir(cls) -> str:
        """저장된 경로가 없으면 OS 기본 다운로드 폴더를 반환한다.

        Windows + Docker 절대경로 매핑:
            `.env` 의 `DOWNLOAD_DIR=/data/downloads` 는 Docker 컨테이너 기준
            절대경로지만, Windows 네이티브 실행 시 그대로 쓰면 드라이브 루트
            (`D:\\data\\downloads`) 로 해석돼 프로젝트 밖 엉뚱한 위치에 저장
            된다. 이를 프로젝트 루트 `data/downloads/` 로 매핑하여
            `get_data_path()` 와 같은 원칙(Docker 아닌 로컬은 프로젝트 루트)
            을 따른다.
            다른 곳에 저장하려면 `.env` 의 DOWNLOAD_DIR 을 명시적 Windows
            경로 (예: `C:/Users/tgkim/Downloads/study-helper`) 로 지정.
        """
        raw = cls.DOWNLOAD_DIR
        if not raw:
            return _default_download_dir()

        if (
            sys.platform == "win32"
            and (raw.startswith("/") or raw.startswith("\\"))
            and not _is_docker_with_data_volume()
        ):
            # Docker unix 절대경로를 프로젝트 루트 data/ 하위로 리매핑.
            # /data/downloads → <project>/data/downloads
            # /data/logs      → <project>/data/logs
            # /foo/bar        → <project>/data/foo/bar (fallback 매핑)
            project_root = Path(__file__).resolve().parent.parent
            rel = raw.lstrip("/\\")
            if rel.startswith("data/") or rel.startswith("data\\"):
                # `/data/…` 는 이미 `data/…` 와 매핑되도록 중복 접두어 제거
                rel = rel[len("data") :].lstrip("/\\")
                remapped = project_root / "data" / rel if rel else project_root / "data"
            else:
                remapped = project_root / "data" / rel
            resolved = str(remapped.resolve())
            if not cls._drive_root_trap_warned:
                from src.logger import get_logger

                get_logger("config").warning(
                    "DOWNLOAD_DIR=%r 은 Docker unix 절대경로 — Windows 네이티브에서는 "
                    "프로젝트 루트 기반 %r 로 매핑됩니다. 다른 위치를 원하시면 "
                    "`.env` 의 DOWNLOAD_DIR 을 Windows 경로 (예: "
                    "`C:/Users/...`)로 수정하세요.",
                    raw, resolved,
                )
                cls._drive_root_trap_warned = True
            return resolved
        return raw

    # Windows drive-root trap 경고 중복 방지 플래그
    _drive_root_trap_warned: bool = False

    @classmethod
    def save_settings(
        cls,
        download_dir: str,
        download_rule: str,
        stt_enabled: bool,
        ai_enabled: bool,
        ai_agent: str,
        api_key: str,
        gemini_model: str = "",
        summary_prompt_extra: str = "",
    ) -> None:
        """설정 항목을 .env 파일에 저장한다."""
        cls.DOWNLOAD_DIR = download_dir
        cls.DOWNLOAD_RULE = download_rule
        cls.STT_ENABLED = "true" if stt_enabled else "false"
        cls.AI_ENABLED = "true" if ai_enabled else "false"
        cls.AI_AGENT = ai_agent
        cls.SUMMARY_PROMPT_EXTRA = summary_prompt_extra
        if gemini_model:
            cls.GEMINI_MODEL = gemini_model
        # API 키는 선택한 에이전트에 맞게 저장
        if ai_enabled and ai_agent == "gemini":
            cls.GOOGLE_API_KEY = api_key
        elif ai_enabled and ai_agent == "openai":
            cls.OPENAI_API_KEY = api_key

        to_save: dict = {
            "DOWNLOAD_DIR": download_dir,
            "DOWNLOAD_RULE": download_rule,
            "STT_ENABLED": cls.STT_ENABLED,
            "AI_ENABLED": cls.AI_ENABLED,
            "AI_AGENT": ai_agent,
            "SUMMARY_PROMPT_EXTRA": summary_prompt_extra,
        }
        if gemini_model:
            to_save["GEMINI_MODEL"] = gemini_model
        if ai_enabled and ai_agent == "gemini":
            to_save["GOOGLE_API_KEY"] = encrypt(api_key) if api_key else ""
        elif ai_enabled and ai_agent == "openai":
            to_save["OPENAI_API_KEY"] = encrypt(api_key) if api_key else ""
        cls._save_env(to_save)

    @classmethod
    def save_telegram(cls, enabled: bool, bot_token: str, chat_id: str, auto_delete: bool) -> None:
        """텔레그램 설정을 .env 파일에 저장한다."""
        cls.TELEGRAM_ENABLED = "true" if enabled else "false"
        cls.TELEGRAM_BOT_TOKEN = bot_token
        cls.TELEGRAM_CHAT_ID = chat_id
        cls.TELEGRAM_AUTO_DELETE = "true" if auto_delete else "false"
        cls._save_env(
            {
                "TELEGRAM_ENABLED": cls.TELEGRAM_ENABLED,
                "TELEGRAM_BOT_TOKEN": encrypt(bot_token) if bot_token else "",
                "TELEGRAM_CHAT_ID": chat_id,
                "TELEGRAM_AUTO_DELETE": cls.TELEGRAM_AUTO_DELETE,
            }
        )

    @classmethod
    def save_credentials(cls, user_id: str, password: str) -> None:
        """계정 정보를 암호화해서 .env 파일에 저장"""
        cls.LMS_USER_ID = user_id
        cls.LMS_PASSWORD = password
        cls._save_env(
            {
                "LMS_USER_ID": encrypt(user_id),
                "LMS_PASSWORD": encrypt(password),
            }
        )

    @classmethod
    def _save_env(cls, keys_to_update: dict) -> None:
        """지정한 키/값을 .env 파일에 저장(덮어쓰기)한다.

        M8: 파일 읽기(readlines) 가 lock 밖이면 save_settings/save_telegram/
        save_credentials 동시 호출 시 서로의 변경을 덮어쓴다(lost update).
        `locked_transaction` 으로 **읽기 → merge → 쓰기 전체를 단일 file_lock**
        안에서 수행한다.
        SEC-001 / ARCH-011: atomic_write_text — 0o600 + fsync + replace.
        """
        from src.util.atomic_write import atomic_write_text, locked_transaction

        env_path = _env_path

        def _load_lines() -> list[str]:
            if env_path.exists():
                with open(env_path, encoding="utf-8") as f:
                    return f.readlines()
            return []

        def _merge_and_write(lines: list[str]) -> None:
            updated_keys = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if "=" in stripped and not stripped.startswith("#"):
                    key = stripped.split("=", 1)[0].strip()
                    if key in keys_to_update:
                        new_lines.append(f"{key}={keys_to_update[key]}\n")
                        updated_keys.add(key)
                        continue
                new_lines.append(line)
            for key, value in keys_to_update.items():
                if key not in updated_keys:
                    new_lines.append(f"{key}={value}\n")
            atomic_write_text(env_path, "".join(new_lines), mode=0o600)

        # load_fn 으로 lock 안에서 읽고, save_fn 으로 merge+write — read/write 가
        # 동일 lock 구간. with 본문은 비어있음(추가 mutation 불필요).
        with locked_transaction(env_path, load_fn=_load_lines, save_fn=_merge_and_write):
            pass
